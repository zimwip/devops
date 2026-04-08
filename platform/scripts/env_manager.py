"""env_manager.py — Create, destroy and inspect platform environments.

All environments — dev, staging, prod, and POC — are managed uniformly.
Each environment directory contains:

  envs/{env}/
  ├── envs.yaml               ← environment manifest (type: standard | poc)
  └── {service}/
      ├── version.yaml        ← deployed version state for this service
      └── values.yaml         ← Helm values override (optional)

Standard environments have type: standard.
POC environments have type: poc with TTL fields; the same operations apply.
"""

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
            for e in envs:
                e["_expiry"] = self._expiry_status(e["manifest"])
            print(json.dumps(envs, indent=2))
            return
        col_w = [36, 10, 12, 10, 18, 22]
        header = ["Environment", "Type", "Platform", "Cluster", "Owner", "Expires / status"]
        rows = []
        for e in envs:
            m = e["manifest"]
            env_type = m.get("type", "standard")
            if env_type == "poc":
                expiry = self._expiry_status(m)
                if expiry["status"] == "expired":
                    expires_label = f"EXPIRED ({expiry['days_overdue']}d ago) !!"
                elif expiry["status"] == "warning":
                    expires_label = f"{m.get('expires_at','')[:10]} ({expiry['days_remaining']}d !)"
                else:
                    expires_label = f"{m.get('expires_at','')[:10]} ({expiry.get('days_remaining','?')}d)"
            else:
                expires_label = "permanent"
            cluster = m.get("cluster", self.cfg.default_cluster_dev)
            profile = self.cfg.get_cluster_profile(cluster)
            rows.append([
                e["name"],
                env_type,
                profile.platform,
                cluster[:10],
                m.get("owner", m.get("updated_by", "—"))[:18],
                expires_label[:22],
            ])
        self._print_table(header, rows, col_w)

    def info(self, name: str):
        try:
            manifest = self.cfg.load_env_manifest(name)
        except FileNotFoundError:
            error_exit(f"Environment '{name}' not found.")

        services = self._load_services(name)

        if self.json_output:
            data_out = {
                "name": name,
                "manifest": manifest,
                "services": services,
                "_expiry": self._expiry_status(manifest),
            }
            print(json.dumps(data_out, indent=2))
            return

        env_type = manifest.get("type", "standard")
        cluster  = manifest.get("cluster", self.cfg.default_cluster_dev)
        profile  = self.cfg.get_cluster_profile(cluster)

        print(f"\n  Environment : {name}")
        print(f"  Type        : {env_type}")
        print(f"  Platform    : {profile.platform}")
        print(f"  Cluster     : {cluster}")
        ns_pattern = manifest.get("namespace_pattern", f"{name}-{{service}}")
        print(f"  NS pattern  : {ns_pattern}")

        if env_type == "poc":
            print(f"  Owner       : {manifest.get('owner', '—')}")
            print(f"  Description : {manifest.get('description', '—')}")
            print(f"  Base env    : {manifest.get('base_env', '—')}")
            if manifest.get("contact_slack"):
                print(f"  Slack       : {manifest.get('contact_slack')}")
            expiry = self._expiry_status(manifest)
            expires_str = manifest.get("expires_at", "—")
            if expiry["status"] == "expired":
                print(f"  Expires     : {expires_str[:10]}  !! EXPIRED {expiry['days_overdue']} day(s) ago")
                print(f"  !! Extend with: platform_cli.py env extend --name {name} --ttl-days 14")
                print(f"  !! Or destroy: platform_cli.py env destroy --name {name}")
            elif expiry["status"] == "warning":
                print(f"  Expires     : {expires_str[:10]}  ! expires in {expiry['days_remaining']} day(s)")
            elif expiry["status"] != "permanent":
                print(f"  Expires     : {expires_str[:10]}  ({expiry['days_remaining']} days remaining)")

            if manifest.get("services_modified"):
                print(f"\n  Modified services (from POC branches):")
                for s in manifest["services_modified"]:
                    print(f"    {s['service']:<28} branch: {s.get('source_branch','—')}")

        print(f"\n  {'Service':<28} {'Version':<18} {'Deployed at'}")
        print(f"  {'─' * 70}")
        for svc, d in sorted(services.items()):
            print(f"  {svc:<28} {d.get('image_tag', d.get('version','—')):<18} {d.get('deployed_at','—')}")
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
        contact_slack: str = "",
        force: bool = False,
    ):
        """Create a new environment forked from a base environment.

        Standard and POC environments are created the same way; type: poc adds
        TTL, contact_slack, and services_modified fields to envs.yaml.

        Returns a dict with 'name' and 'warnings' (list of non-fatal issues).
        """
        if ttl_days > 365:
            warn(f"TTL capped at 365 days (requested {ttl_days}).")
            ttl_days = 365

        full_name = name if env_type in ("standard", "fixed") else self._poc_name(name)
        env_path = self.cfg.env_path(full_name)
        if env_path.exists():
            error_exit(f"Environment '{full_name}' already exists.")

        step(f"Forking '{base}' → '{full_name}'")
        try:
            base_manifest = self.cfg.load_env_manifest(base)
            base_services = self._load_services(base)
        except FileNotFoundError:
            error_exit(f"Base environment '{base}' not found.")

        warnings = []

        def _warn(msg):
            warn(msg)
            warnings.append(msg)

        # ── Resolve cluster / platform / registry ──────────────────────────
        resolved_cluster = (
            cluster
            or base_manifest.get("cluster")
            or self.cfg.default_cluster_for(env_type)
        )
        cluster_profile = self.cfg.get_cluster_profile(resolved_cluster)
        resolved_platform = (
            platform
            or cluster_profile.platform
            or base_manifest.get("platform", "openshift")
        )
        if resolved_platform not in PLATFORMS:
            error_exit(
                f"Unknown platform '{resolved_platform}'. "
                f"Valid values: {', '.join(PLATFORMS)}"
            )
        resolved_registry = cluster_profile.registry
        ns_pattern = namespace or f"{full_name}-{{service}}"

        # ── Identity + confirmation disclaimer ─────────────────────────────
        from identity import resolve_identity, format_disclaimer
        from output import confirm_with_actor
        identity = resolve_identity(self.cfg)
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(days=ttl_days)).isoformat() if env_type == "poc" else None
        actions = [
            f"Fork environment '{base}' → '{full_name}'",
            f"Platform: {resolved_platform}  |  Cluster: {resolved_cluster}",
            f"Namespace pattern: {ns_pattern}",
            f"Write envs/{full_name}/envs.yaml + per-service version.yaml files",
            f"Git commit: 'env: create environment {full_name}'",
        ]
        if env_type == "poc":
            actions.append(f"Expires: {expires[:10] if expires else '—'}")

        if not self.dry_run and not self.json_output:
            confirm_with_actor(format_disclaimer(identity, actions), force=force)

        # ── Build envs.yaml ────────────────────────────────────────────────
        git_user = self._git_user()
        manifest: dict = {
            "name":              full_name,
            "type":              "poc" if env_type == "poc" else "standard",
            "cluster":           resolved_cluster,
            "namespace_pattern": ns_pattern,
            "platform":          resolved_platform,
            "registry":          resolved_registry,
            "created_at":        now.isoformat(),
            "updated_at":        now.isoformat(),
            "updated_by":        f"{git_user} via platform-cli",
        }
        if env_type == "poc":
            manifest.update({
                "base_env":          base,
                "owner":             owner or git_user,
                "description":       description,
                "expires_at":        expires,
                "contact_slack":     contact_slack,
                "branch_convention": f"poc/{name}",
                "services_modified": [],
                "services_stable":   [],
            })

        if not self.dry_run:
            self.cfg.save_env_manifest(full_name, manifest)
            # Copy per-service version.yaml files from base env
            for svc, svc_data in (base_services or {}).items():
                self.cfg.save_service_version(full_name, svc, dict(svc_data))
                # Copy values.yaml if present
                base_values = self.cfg.load_service_values(base, svc)
                if base_values:
                    self.cfg.save_service_values(full_name, svc, base_values)

            commit_ok = self._git_commit(
                f"env: create environment '{full_name}'",
                collect_warnings=warnings,
            )
            if not commit_ok:
                _warn(
                    "Could not auto-commit to platform-config Git repo. "
                    f"Run: git add envs/ && git commit -m 'env: create {full_name}'"
                )

        success(f"Environment '{full_name}' created.")
        print(f"  Platform  : {resolved_platform}")
        print(f"  Cluster   : {resolved_cluster}")
        print(f"  Registry  : {resolved_registry}")
        print(f"  NS pattern: {ns_pattern}")
        if env_type == "poc":
            print(f"  Expires   : {expires[:10] if expires else '—'}")
        print(f"  Based on  : {base}")
        if warnings:
            print()
            print("  Warnings:")
            for w in warnings:
                print(f"  !  {w}")
        sep = chr(92) if IS_WINDOWS else "/"
        print()
        print(f"  Deploy a service:  platform_cli.py deploy --env {full_name} --service <n> --version <ver>")
        print(f"  Destroy when done: platform_cli.py env destroy --name {full_name}")

        return {"name": full_name, "warnings": warnings}

    def destroy(self, name: str, force: bool = False):
        env_path = self.cfg.env_path(name)
        if not env_path.exists():
            error_exit(f"Environment '{name}' not found.")
        try:
            manifest = self.cfg.load_env_manifest(name)
            env_type = manifest.get("type", "standard")
        except Exception:
            env_type = "unknown"

        if env_type == "standard":
            error_exit(
                f"Cannot destroy standard environment '{name}'. "
                "Only POC environments can be destroyed via this command."
            )

        if not force:
            from identity import resolve_identity, format_disclaimer
            from output import confirm_with_actor
            identity = resolve_identity(self.cfg)
            ns_pattern = manifest.get("namespace_pattern", f"{name}-{{service}}")
            actions = [
                f"Delete POC environment '{name}' from platform-config",
                f"Git commit: 'env: destroy POC environment {name}'",
                f"Delete namespaces matching pattern '{ns_pattern}'",
            ]
            confirm_with_actor(format_disclaimer(identity, actions), force=False)

        step(f"Destroying environment '{name}'")
        if not self.dry_run:
            self._delete_namespaces(name, manifest)
            import shutil
            shutil.rmtree(env_path)
            self._git_commit(f"env: destroy POC environment '{name}'")

        success(f"Environment '{name}' destroyed.")

    def diff(self, env_from: str, env_to: str):
        try:
            from_services = self._load_services(env_from)
            to_services   = self._load_services(env_to)
            from_manifest = self.cfg.load_env_manifest(env_from)
            to_manifest   = self.cfg.load_env_manifest(env_to)
        except FileNotFoundError as e:
            error_exit(str(e))

        all_svcs = sorted(set(list(from_services) + list(to_services)))
        results = []
        for svc in all_svcs:
            fv = from_services.get(svc, {}).get("image_tag", from_services.get(svc, {}).get("version", "—"))
            tv = to_services.get(svc, {}).get("image_tag", to_services.get(svc, {}).get("version", "—"))
            results.append({"service": svc, env_from: fv, env_to: tv, "changed": fv != tv})

        if self.json_output:
            print(json.dumps(results, indent=2))
            return

        fp = from_manifest.get("platform", "?")
        fc = from_manifest.get("cluster", "?")
        tp = to_manifest.get("platform", "?")
        tc = to_manifest.get("cluster", "?")
        print(f"\n  Diff: {env_from} [{fp}/{fc}]  →  {env_to} [{tp}/{tc}]\n")
        col_w = [28, 18, 18, 8]
        header = ["Service", env_from, env_to, "Changed"]
        rows = [
            [r["service"], r[env_from], r[env_to], "yes" if r["changed"] else ""]
            for r in results
        ]
        self._print_table(header, rows, col_w)

    def extend(self, name: str, ttl_days: int = 14):
        """Postpone the TTL of a POC environment by adding ttl_days to expires_at.
        The new expiry cannot exceed 365 days from today.
        """
        try:
            manifest = self.cfg.load_env_manifest(name)
        except FileNotFoundError:
            error_exit(f"Environment '{name}' not found.")

        if manifest.get("type", "standard") != "poc":
            error_exit(f"'{name}' is not a POC environment — only POC environments have a TTL.")

        now = datetime.now(timezone.utc)
        current_expires = manifest.get("expires_at")
        if current_expires:
            try:
                base_dt = datetime.fromisoformat(current_expires.replace("Z", "+00:00"))
                base_dt = max(base_dt, now)
            except ValueError:
                base_dt = now
        else:
            base_dt = now

        new_expires = base_dt + timedelta(days=ttl_days)
        max_expires = now + timedelta(days=365)
        if new_expires > max_expires:
            warn(f"New expiry capped at 365 days from today ({max_expires.date()}).")
            new_expires = max_expires

        manifest["expires_at"] = new_expires.isoformat()
        manifest["updated_at"] = now.isoformat()
        manifest["updated_by"] = f"{self._git_user()} via platform-cli (extend)"

        if not self.dry_run:
            self.cfg.save_env_manifest(name, manifest)
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

    def _load_services(self, env_name: str) -> dict:
        """Return {service: version_data} for all services in the environment."""
        services: dict = {}
        for svc in self.cfg.list_services_in_env(env_name):
            try:
                services[svc] = self.cfg.load_service_version(env_name, svc)
            except FileNotFoundError:
                pass
        # Also check legacy versions.yaml if no per-service dirs found
        if not services:
            legacy = self.cfg._try_load_legacy(env_name)
            if legacy:
                for svc, d in (legacy.get("services") or {}).items():
                    services[svc] = {
                        "service":     svc,
                        "image_tag":   d.get("version", ""),
                        "image":       d.get("image", ""),
                        "deployed_at": d.get("deployed_at", ""),
                        "deployed_by": d.get("deployed_by", ""),
                        "health":      d.get("health", ""),
                    }
        return services

    def _expiry_status(self, manifest: dict) -> dict:
        """Compute expiry status for a POC environment manifest.

        Returns:
          {"status": "ok" | "warning" | "expired" | "permanent"}
          plus "days_remaining" and "days_overdue" for poc envs.
        """
        if manifest.get("type", "standard") != "poc":
            return {"status": "permanent"}

        expires_str = manifest.get("expires_at")
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
        """Commit changes to the platform-config repo and push to remote."""
        def _warn(msg):
            if collect_warnings is not None:
                collect_warnings.append(msg)
            else:
                warn(msg)

        try:
            run(["git", "rev-parse", "--git-dir"],
                cwd=self.cfg.root, check=True, capture_output=True)
        except Exception:
            _warn(
                "Not a git repository — environment change not tracked. "
                "Run bootstrap.sh first or: git init && git add --all && git commit."
            )
            return False

        try:
            run(["git", "add", "envs/", "platform.yaml"],
                cwd=self.cfg.root, check=True, capture_output=True)
            run(["git", "commit", "-m", message],
                cwd=self.cfg.root, check=True, capture_output=True)
        except subprocess.CalledProcessError:
            _warn("Could not auto-commit. Please commit the env changes manually.")
            return False

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
                return True

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

    def _delete_namespaces(self, name: str, manifest: dict):
        """Delete all cluster namespaces matching the environment's pattern."""
        kube = kubectl_executable()
        if not kube:
            warn("kubectl/oc not found — skipping namespace deletion. Run manually:")
            ns_pattern = manifest.get("namespace_pattern", f"{name}-{{service}}")
            warn(f"  kubectl delete namespace -l env={name}")
            return

        # Get all namespaces matching the env prefix
        try:
            result = run(
                [kube, "get", "namespaces", "-o",
                 "jsonpath={.items[*].metadata.name}"],
                capture_output=True, text=True,
            )
            all_ns = result.stdout.strip().split()
        except Exception:
            all_ns = []

        prefix = name + "-"
        matching = [ns for ns in all_ns if ns.startswith(prefix) or ns == name]

        if not matching:
            step(f"No namespaces found matching '{prefix}*' — nothing to delete.")
            return

        for ns in matching:
            cmd = [kube, "delete", "namespace", ns, "--ignore-not-found"]
            try:
                run(cmd, check=True, capture_output=True)
                step(f"Deleted namespace: {ns}")
            except subprocess.CalledProcessError as e:
                warn(f"Namespace deletion failed for '{ns}': "
                     f"{e.stderr.decode() if e.stderr else str(e)}")

    def _load_all_envs(self) -> list[dict]:
        result = []
        for env_name in self.cfg.list_envs():
            try:
                manifest = self.cfg.load_env_manifest(env_name)
            except Exception:
                manifest = {"name": env_name, "type": "standard"}
            result.append({"name": env_name, "manifest": manifest})
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
