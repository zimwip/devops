"""env_manager.py — Create, destroy and inspect platform environments."""

import copy
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from config import PlatformConfig, PLATFORMS
from compat import run, kubectl_executable, IS_WINDOWS
from output import out, step, success, warn, error_exit, confirm


class EnvManager:
    def __init__(self, cfg: PlatformConfig, dry_run=False, json_output=False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.json_output = json_output

    # ─────────────────────────────────────────────────────────────────────────
    # PUBLIC
    # ─────────────────────────────────────────────────────────────────────────

    def list_envs(self):
        envs = self._load_all_envs()
        if self.json_output:
            # Enrich with expiry status
            for e in envs:
                e["_expiry"] = self._expiry_status(e)
            print(json.dumps(envs, indent=2))
            return
        col_w = [36, 10, 12, 10, 18, 22]
        header = ["Environment", "Type", "Platform", "Cluster", "Owner", "Expires / status"]
        rows = []
        for e in envs:
            m = e.get("_meta", {})
            env_type = m.get("env_type", "fixed")
            if env_type == "poc":
                expiry = self._expiry_status(e)
                if expiry["status"] == "expired":
                    expires_label = f"EXPIRED ({expiry['days_overdue']}d ago) !!"
                elif expiry["status"] == "warning":
                    expires_label = f"{m.get('expires_at','')[:10]} ({expiry['days_remaining']}d !)"
                else:
                    expires_label = f"{m.get('expires_at','')[:10]} ({expiry.get('days_remaining','?')}d)"
            else:
                expires_label = "permanent"
            rows.append([
                e["name"],
                env_type,
                m.get("platform", "openshift"),
                m.get("cluster", "—")[:10],
                m.get("owner", m.get("updated_by", "—"))[:18],
                expires_label[:22],
            ])
        self._print_table(header, rows, col_w)

    def info(self, name: str):
        try:
            data = self.cfg.load_versions(name)
        except FileNotFoundError:
            error_exit(f"Environment '{name}' not found.")
        if self.json_output:
            # Inject expiry_status into JSON output
            data_out = {"name": name, **data}
            data_out["_expiry"] = self._expiry_status(data)
            print(json.dumps(data_out, indent=2))
            return
        m = data.get("_meta", {})
        print(f"\n  Environment : {name}")
        print(f"  Type        : {m.get('env_type', 'fixed')}")
        print(f"  Platform    : {m.get('platform', 'openshift')}")
        print(f"  Cluster     : {m.get('cluster', '—')}")
        ns = m.get("namespace", "—")
        ns_src = " (provided externally)" if m.get("namespace_provided") else " (auto-generated)"
        print(f"  Namespace   : {ns}{ns_src}")
        if m.get("env_type") == "poc":
            print(f"  Owner       : {m.get('owner', '—')}")
            print(f"  Description : {m.get('description', '—')}")
            print(f"  Base env    : {m.get('base_env', '—')}")
            # Expiry warning
            expiry = self._expiry_status(data)
            expires_str = m.get('expires_at', '—')
            if expiry["status"] == "expired":
                print(f"  Expires     : {expires_str[:10]}  !! EXPIRED {expiry['days_overdue']} day(s) ago")
                print(f"  !! This environment is past its TTL. "
                      f"Extend with: python scripts/platform_cli.py env extend --name {name} --ttl-days 14")
                print(f"  !! Or destroy: python scripts/platform_cli.py env destroy --name {name}")
            elif expiry["status"] == "warning":
                print(f"  Expires     : {expires_str[:10]}  ! expires in {expiry['days_remaining']} day(s)")
            else:
                print(f"  Expires     : {expires_str[:10]}  ({expiry['days_remaining']} days remaining)")
        print(f"\n  {'Service':<28} {'Version':<18} {'Deployed at'}")
        print(f"  {'─' * 70}")
        for svc, d in (data.get("services") or {}).items():
            print(f"  {svc:<28} {d.get('version','—'):<18} {d.get('deployed_at','—')}")
        print()

    def create(
        self,
        name: str,
        env_type: str,
        base: str,
        namespace: str = None,
        cluster: str = None,
        platform: str = None,
        owner: str = None,
        description: str = "",
        ttl_days: int = 14,
        force: bool = False,
    ):
        """
        Create a new environment (typically a POC) forked from a base env.
        Returns a dict with 'name' and 'warnings' (list of non-fatal issues).

        TTL cap: max 365 days. Expiry is a soft deadline — no automatic destruction.
        A warning is shown in `env info` and the dashboard when expired.
        Use `env extend` to postpone the deadline.
        """
        # Cap TTL
        if ttl_days > 365:
            warn(f"TTL capped at 365 days (requested {ttl_days}).")
            ttl_days = 365
        full_name = name if env_type == "fixed" else self._poc_name(name)
        env_path = self.cfg.env_path(full_name)
        if env_path.exists():
            error_exit(f"Environment '{full_name}' already exists.")

        step(f"Forking '{base}' -> '{full_name}'")
        try:
            base_data = self.cfg.load_versions(base)
        except FileNotFoundError:
            error_exit(f"Base environment '{base}' not found.")

        base_meta = base_data.get("_meta", {})
        warnings = []

        def _warn(msg):
            warn(msg)
            warnings.append(msg)

        # ── Resolve cluster ────────────────────────────────────────────────
        resolved_cluster = (
            cluster
            or base_meta.get("cluster")
            or self.cfg.default_cluster_for(env_type)
        )

        # ── Resolve platform ───────────────────────────────────────────────
        cluster_profile = self.cfg.get_cluster_profile(resolved_cluster)
        resolved_platform = (
            platform
            or cluster_profile.platform
            or base_meta.get("platform", "openshift")
        )

        if resolved_platform not in PLATFORMS:
            error_exit(
                f"Unknown platform '{resolved_platform}'. "
                f"Valid values: {', '.join(PLATFORMS)}"
            )

        # ── Resolve registry ───────────────────────────────────────────────
        resolved_registry = cluster_profile.registry

        # ── Resolve namespace ──────────────────────────────────────────────
        resolved_namespace = namespace or f"platform-{full_name}"

        # ── Identity + confirmation disclaimer ────────────────────────────
        from identity import resolve_identity, format_disclaimer
        from output import confirm_with_actor
        identity = resolve_identity(self.cfg)
        actions = [
            f"Fork environment '{base}' → '{full_name}'",
            f"Platform: {resolved_platform}  |  Cluster: {resolved_cluster}",
            f"Namespace: {resolved_namespace}" + (" (provided)" if namespace else " (auto)"),
            f"Write envs/{full_name}/versions.yaml to platform-config",
            f"Git commit: 'env: create environment {full_name}'",
        ]
        if env_type == "poc":
            actions.append(f"Expires: {(datetime.now(timezone.utc) + timedelta(days=ttl_days)).strftime('%Y-%m-%d')}")

        if not self.dry_run and not self.json_output:
            confirm_with_actor(
                format_disclaimer(identity, actions),
                force=force,
            )

        new_data = copy.deepcopy(base_data)
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(days=ttl_days)).isoformat()
        git_user = self._git_user()
        new_data["_meta"] = {
            "updated_at": now.isoformat(),
            "updated_by": f"{git_user} via platform-cli",
            "commit": "bootstrap",
            "env_type": env_type,
            "base_env": base,
            "owner": owner or git_user,
            "description": description,
            "expires_at": expires,
            "platform": resolved_platform,
            "cluster": resolved_cluster,
            "registry": resolved_registry,
            "namespace": resolved_namespace,
            "namespace_provided": namespace is not None,
            "branch_convention": f"poc/{name}",
        }

        if not self.dry_run:
            self.cfg.save_versions(full_name, new_data)
            commit_ok = self._git_commit(
                f"env: create environment '{full_name}'",
                collect_warnings=warnings,
            )
            if not commit_ok:
                _warn(
                    "Could not auto-commit to platform-config Git repo. "
                    "Run: git add envs/ && git commit -m 'env: create "
                    f"{full_name}'"
                )

        success(f"Environment '{full_name}' created.")
        print(f"  Platform  : {resolved_platform}")
        print(f"  Cluster   : {resolved_cluster}")
        print(f"  Registry  : {resolved_registry}")
        print(f"  Namespace : {resolved_namespace}"
              + (" (provided)" if namespace else " (auto-generated)"))
        if env_type == "poc":
            print(f"  Expires   : {expires[:10]}")
        print(f"  Based on  : {base}")
        if warnings:
            print()
            print("  Warnings:")
            for w in warnings:
                print(f"  !  {w}")
        sep = chr(92) if IS_WINDOWS else "/"
        print()
        print(f"  Deploy a service:  python scripts{sep}platform_cli.py deploy --env {full_name} --service <n> --version <ver>")
        print(f"  Destroy when done: python scripts{sep}platform_cli.py env destroy --name {full_name}")

        return {"name": full_name, "warnings": warnings}

    def destroy(self, name: str, force: bool = False):
        env_path = self.cfg.env_path(name)
        if not env_path.exists():
            error_exit(f"Environment '{name}' not found.")
        try:
            data = self.cfg.load_versions(name)
            env_type = data.get("_meta", {}).get("env_type", "fixed")
        except Exception:
            env_type = "unknown"

        if env_type == "fixed":
            error_exit(
                f"Cannot destroy fixed environment '{name}'. "
                "Only POC environments can be destroyed."
            )

        if not force:
            from identity import resolve_identity, format_disclaimer
            from output import confirm_with_actor
            identity = resolve_identity(self.cfg)
            namespace = data.get("_meta", {}).get("namespace", f"platform-{name}")
            ns_provided = data.get("_meta", {}).get("namespace_provided", False)
            actions = [
                f"Delete POC environment '{name}' from platform-config",
                f"Git commit: 'env: destroy POC environment {name}'",
            ]
            if ns_provided:
                actions.append(f"Namespace '{namespace}' will NOT be deleted (externally provided)")
            else:
                actions.append(f"Delete OpenShift namespace '{namespace}' (oc delete namespace)")

            confirm_with_actor(format_disclaimer(identity, actions), force=False)

        step(f"Destroying environment '{name}'")
        if not self.dry_run:
            ns_provided = data.get("_meta", {}).get("namespace_provided", False)
            if ns_provided:
                namespace = data.get("_meta", {}).get("namespace", f"platform-{name}")
                warn(f"Namespace '{namespace}' was provided externally — skipping deletion.")
                warn("Remove deployed Helm releases manually before reusing the namespace.")
            else:
                self._delete_namespace(name, data)
            import shutil
            shutil.rmtree(env_path)
            self._git_commit(f"env: destroy POC environment '{name}'")

        success(f"Environment '{name}' destroyed.")

    def diff(self, env_from: str, env_to: str):
        try:
            from_data = self.cfg.load_versions(env_from)
            to_data = self.cfg.load_versions(env_to)
        except FileNotFoundError as e:
            error_exit(str(e))

        from_svcs = from_data.get("services", {})
        to_svcs = to_data.get("services", {})
        all_svcs = sorted(set(list(from_svcs) + list(to_svcs)))
        results = []
        for svc in all_svcs:
            fv = from_svcs.get(svc, {}).get("version", "—")
            tv = to_svcs.get(svc, {}).get("version", "—")
            results.append({"service": svc, env_from: fv, env_to: tv, "changed": fv != tv})

        if self.json_output:
            print(json.dumps(results, indent=2))
            return

        # Show platform/cluster context for each side
        fm = from_data.get("_meta", {})
        tm = to_data.get("_meta", {})
        print(f"\n  Diff: {env_from} [{fm.get('platform','?')}/{fm.get('cluster','?')}]"
              f"  →  {env_to} [{tm.get('platform','?')}/{tm.get('cluster','?')}]\n")
        col_w = [28, 18, 18, 8]
        header = ["Service", env_from, env_to, "Changed"]
        rows = [
            [r["service"], r[env_from], r[env_to], "yes" if r["changed"] else ""]
            for r in results
        ]
        self._print_table(header, rows, col_w)

    def extend(self, name: str, ttl_days: int = 14):
        """
        Postpone the TTL of a POC environment by adding `ttl_days` to the
        current `expires_at`. The new expiry cannot exceed 365 days from today.
        """
        try:
            data = self.cfg.load_versions(name)
        except FileNotFoundError:
            error_exit(f"Environment '{name}' not found.")

        meta = data.get("_meta", {})
        if meta.get("env_type", "fixed") != "poc":
            error_exit(f"'{name}' is not a POC environment — only POC environments have a TTL.")

        now = datetime.now(timezone.utc)
        current_expires = meta.get("expires_at")
        if current_expires:
            try:
                base_dt = datetime.fromisoformat(current_expires.replace("Z", "+00:00"))
                # If already expired, extend from now; if still valid, extend from current expiry
                base_dt = max(base_dt, now)
            except ValueError:
                base_dt = now
        else:
            base_dt = now

        new_expires = base_dt + timedelta(days=ttl_days)
        # Hard cap: max 365 days from today
        max_expires = now + timedelta(days=365)
        if new_expires > max_expires:
            warn(f"New expiry capped at 365 days from today ({max_expires.date()}).")
            new_expires = max_expires

        meta["expires_at"] = new_expires.isoformat()
        meta["updated_at"] = now.isoformat()
        git_user = self._git_user()
        meta["updated_by"] = f"{git_user} via platform-cli (extend)"
        data["_meta"] = meta

        if not self.dry_run:
            self.cfg.save_versions(name, data)
            self._git_commit(f"env: extend TTL for '{name}' until {new_expires.date()}")

        success(f"TTL extended for '{name}'.")
        print(f"  New expiry : {new_expires.date()}")
        days_left = (new_expires - now).days
        print(f"  Days left  : {days_left}")

        if self.json_output:
            print(json.dumps({
                "name": name,
                "expires_at": new_expires.isoformat(),
                "days_remaining": days_left,
            }, indent=2))

    # ─────────────────────────────────────────────────────────────────────────
    # PRIVATE
    # ─────────────────────────────────────────────────────────────────────────

    def _expiry_status(self, data: dict) -> dict:
        """
        Compute expiry status for a POC environment.
        Returns a dict with:
          status: "ok" | "warning" (≤7 days) | "expired"
          days_remaining: int (negative if expired)
          days_overdue: int (0 if not expired)
        """
        meta = data.get("_meta", {})
        if meta.get("env_type") != "poc":
            return {"status": "permanent"}

        expires_str = meta.get("expires_at")
        if not expires_str:
            return {"status": "unknown"}

        try:
            expires_dt = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
        except ValueError:
            return {"status": "unknown"}

        now = datetime.now(timezone.utc)
        delta_days = (expires_dt - now).days

        if delta_days < 0:
            return {"status": "expired", "days_remaining": delta_days, "days_overdue": abs(delta_days)}
        elif delta_days <= 7:
            return {"status": "warning", "days_remaining": delta_days, "days_overdue": 0}
        else:
            return {"status": "ok", "days_remaining": delta_days, "days_overdue": 0}

    def _poc_name(self, name: str) -> str:
        date = datetime.now(timezone.utc).strftime("%Y%m%d")
        slug = name.lower().replace(" ", "-").replace("_", "-")
        return f"poc-{slug}-{date}"

    def _git_user(self) -> str:
        try:
            result = run(
                ["git", "config", "user.email"],
                capture_output=True, text=True, cwd=self.cfg.root
            )
            return result.stdout.strip() or "unknown"
        except Exception:
            return "unknown"

    def _git_commit(self, message: str, collect_warnings: list = None) -> bool:
        """Commit changes to the platform-config repo and push to remote.

        Staged paths: envs/ + platform.yaml (cluster profiles, defaults).

        Push strategy: pull --rebase before push so parallel operations
        (CI deploys, other team members) do not cause non-fast-forward errors.
        The local commit is always preserved even when push fails.
        """
        def _warn(msg):
            if collect_warnings is not None:
                collect_warnings.append(msg)
            else:
                warn(msg)

        # Guard: must be inside a git repo
        try:
            run(["git", "rev-parse", "--git-dir"],
                cwd=self.cfg.root, check=True, capture_output=True)
        except Exception:
            _warn(
                "Not a git repository — environment change not tracked. "
                "Run bootstrap.sh first or: git init && git add --all && git commit."
            )
            return False

        # Stage envs/ and platform.yaml
        try:
            run(["git", "add", "envs/", "platform.yaml"],
                cwd=self.cfg.root, check=True, capture_output=True)
            run(["git", "commit", "-m", message],
                cwd=self.cfg.root, check=True, capture_output=True)
        except subprocess.CalledProcessError:
            _warn("Could not auto-commit. Please commit the env changes manually.")
            return False

        # Push to remote if one is configured
        try:
            has_remote = run(
                ["git", "remote"],
                cwd=self.cfg.root, capture_output=True, text=True,
            ).stdout.strip()
            if not has_remote:
                return True

            branch = run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=self.cfg.root, capture_output=True, text=True,
            ).stdout.strip()

            # Pull --rebase to reconcile commits from parallel operations
            try:
                run(
                    ["git", "pull", "--rebase", "origin", branch],
                    cwd=self.cfg.root, check=True, capture_output=True,
                )
            except subprocess.CalledProcessError as pull_err:
                stderr = pull_err.stderr.decode(errors="replace").strip() \
                    if pull_err.stderr else ""
                _warn(
                    f"git pull --rebase failed before push: {stderr or pull_err}. "
                    f"Local commit saved. Resolve manually: "
                    f"git pull --rebase origin {branch} && git push origin {branch}"
                )
                return True  # local commit succeeded

            run(
                ["git", "push", "origin", branch],
                cwd=self.cfg.root, check=True, capture_output=True,
            )
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode(errors="replace").strip() if e.stderr else str(e)
            _warn(
                f"Could not push to remote: {stderr}. "
                f"Local commit was created — push manually: git push origin {branch}"
            )

        return True


    def _delete_namespace(self, name: str, data: dict):
        namespace = data.get("_meta", {}).get("namespace", f"platform-{name}")
        kube = kubectl_executable()
        if not kube:
            warn("kubectl/oc not found — skipping namespace deletion. Run manually:")
            warn(f"  kubectl delete namespace {namespace} --ignore-not-found")
            return
        cmd = [kube, "delete", "namespace", namespace, "--ignore-not-found"]
        try:
            run(cmd, check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            warn(f"Namespace deletion failed: {e.stderr.decode() if e.stderr else str(e)}")

    def _load_all_envs(self) -> list[dict]:
        result = []
        for env_name in self.cfg.list_envs():
            try:
                data = self.cfg.load_versions(env_name)
                data["name"] = env_name
                result.append(data)
            except Exception:
                result.append({"name": env_name, "_meta": {}})
        return result

    def _print_table(self, header, rows, col_widths):
        def fmt(row):
            return "  " + "  ".join(str(v).ljust(w) for v, w in zip(row, col_widths))
        sep = "  " + "  ".join("-" * w for w in col_widths)
        print()
        print(fmt(header))
        print(sep)
        for row in rows:
            print(fmt(row))
        print()
