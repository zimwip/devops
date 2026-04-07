"""
deployer.py — Platform-aware deployment driver.

Supports two target platforms:
  openshift  — uses `oc`/`kubectl` + Helm, login via oc CLI context
  aws        — uses `kubectl` + Helm, auth via AWS CLI / kubeconfig EKS context,
               ECR image pull (registry auth handled separately)

Deploy path selection:
  1. If JENKINS_TOKEN is set → trigger a Jenkins parameterised build
     (Jenkins itself handles the platform-specific steps)
  2. Otherwise → local direct deploy using Helm + oc/kubectl
     (useful for POC environments and local testing)

In both cases, versions.yaml is updated after a successful trigger.
"""

import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

from config import PlatformConfig, ClusterProfile
from compat import run, helm_executable, kubectl_executable, IS_WINDOWS
from output import step, success, warn, error_exit


class Deployer:
    def __init__(self, cfg: PlatformConfig, dry_run=False, json_output=False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.json_output = json_output

    # ─────────────────────────────────────────────────────────────────────────
    # PUBLIC
    # ─────────────────────────────────────────────────────────────────────────

    def deploy(self, env: str, service: str, version: str,
               wait: bool = False, force: bool = False):
        step(f"Deploying {service}:{version} → {env}")

        try:
            env_data = self.cfg.load_versions(env)
        except FileNotFoundError:
            error_exit(f"Environment '{env}' not found.")

        meta = env_data.get("_meta", {})
        namespace    = meta.get("namespace", f"platform-{env}")
        cluster_name = meta.get("cluster", self.cfg.default_cluster_dev)
        profile      = self.cfg.get_cluster_profile(cluster_name)
        registry     = meta.get("registry") or profile.registry
        image        = f"{registry}/{service}:{version}"

        # ── Identity + confirmation disclaimer ────────────────────────────
        from identity import resolve_identity, format_disclaimer
        from output import confirm_with_actor
        identity = resolve_identity(self.cfg)
        actions = [
            f"Deploy {service}:{version}",
            f"Platform : {profile.platform}  |  Cluster: {cluster_name}",
            f"Namespace: {namespace}",
            f"Image    : {image}",
        ]
        if self.cfg.jenkins_token:
            actions.append("Via Jenkins parameterised build")
        else:
            actions.append(f"Direct Helm deploy (helm upgrade --install)")
        actions.append(f"Update envs/{env}/versions.yaml")

        if not self.dry_run and not self.json_output:
            confirm_with_actor(
                format_disclaimer(identity, actions),
                force=force,
            )

        if self.dry_run:
            self._print_dry_run(profile, service, version, namespace, image)
            return

        # ── Deploy path ────────────────────────────────────────────────────
        if self.cfg.jenkins_token:
            # Jenkins handles platform-specific auth internally
            self._trigger_jenkins(service, version, env, namespace,
                                  profile.platform, cluster_name)
        else:
            # Direct local deploy: fetch chart from service repo first
            if profile.is_openshift:
                self._deploy_openshift(profile, service, version, namespace, wait, env)
            else:
                self._deploy_aws(profile, service, version, namespace, wait, env)

        # ── Update versions.yaml + commit ─────────────────────────────────────
        now = datetime.now(timezone.utc).isoformat()
        actor = identity.display_name if identity.display_email == "" \
            else f"{identity.display_name} <{identity.display_email}>"
        env_data.setdefault("services", {})[service] = {
            "version": version,
            "image": image,
            "deployed_at": now,
            "deployed_by": actor,
            "health": "deploying",
        }
        env_data["_meta"]["updated_at"] = now
        env_data["_meta"]["updated_by"] = actor
        # Record commit SHA on the meta so history can link back to it
        env_data["_meta"]["commit"] = self._git_head_sha()
        self.cfg.save_versions(env, env_data)

        # Git-commit the updated versions.yaml so it appears in history
        self._git_commit(
            f"deploy: {service}:{version} → {env} [{profile.platform}/{cluster_name}]"
        )

        result = {
            "env": env, "service": service, "version": version,
            "image": image, "namespace": namespace,
            "platform": profile.platform, "cluster": cluster_name,
            "deployed_at": now,
        }
        if self.json_output:
            print(json.dumps(result, indent=2))
        else:
            success(f"{service}:{version} deployed to {env}")
            print(f"  Platform  : {profile.platform}")
            print(f"  Cluster   : {cluster_name}")
            print(f"  Namespace : {namespace}")
            print(f"  Image     : {image}")

    # ─────────────────────────────────────────────────────────────────────────
    # PUBLIC — deployment requests (GitOps pull model)
    # ─────────────────────────────────────────────────────────────────────────

    def request_deploy(self, env: str, service: str, version: str,
                       force: bool = False):
        """Declare a desired deployment in versions.yaml (GitOps pull model).

        Writes a `requested_deployments` entry for the service. Jenkins will
        pick this up on the next successful build when version == 'latest' and
        auto == True, then call execute_deploy_request().
        """
        from identity import resolve_identity

        step(f"Requesting deployment: {service}@{version} → {env}")

        try:
            env_data = self.cfg.load_versions(env)
        except FileNotFoundError:
            error_exit(f"Environment '{env}' not found.")

        identity = resolve_identity(self.cfg)
        actor = (
            identity.display_name
            if identity.display_email == ""
            else f"{identity.display_name} <{identity.display_email}>"
        )
        now = datetime.now(timezone.utc).isoformat()

        env_data["requested_deployments"][service] = {
            "requested_version": version,
            "requested_at": now,
            "requested_by": actor,
            "auto": version == "latest",
            "status": "pending",
            "fulfilled_version": None,
            "fulfilled_at": None,
        }
        self.cfg.save_versions(env, env_data)
        self._git_commit(f"deploy-request: {service}@{version} → {env}")
        success(f"Deployment request recorded: {service}@{version} in {env}")

    def cancel_deploy_request(self, env: str, service: str, force: bool = False):
        """Remove a pending deployment request from versions.yaml."""
        step(f"Cancelling deployment request: {service} in {env}")

        try:
            env_data = self.cfg.load_versions(env)
        except FileNotFoundError:
            error_exit(f"Environment '{env}' not found.")

        if service not in env_data.get("requested_deployments", {}):
            error_exit(f"No pending deployment request for '{service}' in '{env}'.")

        del env_data["requested_deployments"][service]
        self.cfg.save_versions(env, env_data)
        self._git_commit(f"deploy-cancel: {service} in {env}")
        success(f"Deployment request cancelled: {service} in {env}")

    def execute_deploy_request(self, env: str, service: str, version: str,
                               force: bool = False, wait: bool = False):
        """Execute a pending deployment request with a resolved version.

        Called by Jenkins after resolving 'latest' to the actual built tag.
        Runs the real deploy(), then marks the request as fulfilled.
        """
        # Run the actual deployment
        self.deploy(env=env, service=service, version=version,
                    wait=wait, force=force)

        # Mark the request as fulfilled (re-load after deploy() wrote to it)
        try:
            env_data = self.cfg.load_versions(env)
        except FileNotFoundError:
            return  # already deployed; nothing to update

        requests = env_data.get("requested_deployments", {})
        if service in requests:
            requests[service]["status"] = "fulfilled"
            requests[service]["fulfilled_version"] = version
            requests[service]["fulfilled_at"] = datetime.now(timezone.utc).isoformat()
            self.cfg.save_versions(env, env_data)
            self._git_commit(f"deploy-fulfilled: {service}@{version} → {env}")

    def _git_commit(self, message: str):
        """Commit the updated versions.yaml and push to remote.

        Push strategy:
        - If the remote is ahead (diverged), pull --rebase first to avoid
          non-fast-forward errors.  This is safe because only envs/ is touched
          by parallel deploy operations and merge conflicts in YAML are rare.
        - Push failure is surfaced as a visible warning, not silently dropped.
          The local commit is always preserved so the operator can push manually.
        """
        try:
            run(["git", "add", "envs/"],
                cwd=self.cfg.root, check=True, capture_output=True)
            run(["git", "commit", "-m", message],
                cwd=self.cfg.root, check=True, capture_output=True)
        except Exception:
            return  # not a git repo or nothing to commit

        # Push to remote if one is configured
        try:
            remotes = run(
                ["git", "remote"],
                cwd=self.cfg.root, capture_output=True, text=True,
            ).stdout.strip()
            if not remotes:
                return

            branch = run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=self.cfg.root, capture_output=True, text=True,
            ).stdout.strip()

            # Pull --rebase to reconcile any commits pushed by parallel operations
            # (CI deployments, other team members).  Fail-safe: if rebase fails,
            # skip push rather than force-push.
            try:
                run(
                    ["git", "pull", "--rebase", "origin", branch],
                    cwd=self.cfg.root, check=True, capture_output=True,
                )
            except subprocess.CalledProcessError as pull_err:
                stderr = pull_err.stderr.decode(errors="replace").strip() if pull_err.stderr else ""
                warn(
                    f"git pull --rebase before push failed: {stderr or pull_err}. "
                    "Local commit saved. Resolve manually: "
                    f"git pull --rebase origin {branch} && git push origin {branch}"
                )
                return

            run(
                ["git", "push", "origin", branch],
                cwd=self.cfg.root, check=True, capture_output=True,
            )
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode(errors="replace").strip() if e.stderr else str(e)
            warn(
                f"Could not push to remote: {stderr}. "
                "Local commit was created — push manually: "
                f"git push origin {branch if 'branch' in dir() else 'main'}"
            )

    def _git_head_sha(self) -> str:
        """Return the current HEAD SHA (first 8 chars), or 'unknown'."""
        try:
            result = run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=self.cfg.root, capture_output=True, text=True,
            )
            return result.stdout.strip() or "unknown"
        except Exception:
            return "unknown"

    # ─────────────────────────────────────────────────────────────────────────
    # PRIVATE — dry-run
    # ─────────────────────────────────────────────────────────────────────────

    def _print_dry_run(self, profile: ClusterProfile, service, version,
                       namespace, image):
        print(f"  [dry-run] platform   : {profile.platform}")
        print(f"  [dry-run] cluster    : {profile.name}")
        print(f"  [dry-run] image      : {image}")
        print(f"  [dry-run] namespace  : {namespace}")
        if profile.is_openshift:
            print(f"  [dry-run] oc login --server={profile.api_url}")
            print(f"  [dry-run] oc project {namespace}")
        else:
            print(f"  [dry-run] aws eks update-kubeconfig "
                  f"--region {profile.region} "
                  f"--name {profile.cluster_name}")
        print(f"  [dry-run] git clone --depth 1 <service-repo> → "
              f"envs/<env>/charts/{service}/{version}/")
        print(f"  [dry-run] helm upgrade --install {service} "
              f"envs/<env>/charts/{service}/{version}/helm/")
        print(f"  [dry-run]   --namespace {namespace}")
        print(f"  [dry-run]   --set image.tag={version}")

    # ─────────────────────────────────────────────────────────────────────────
    # PRIVATE — Jenkins path
    # ─────────────────────────────────────────────────────────────────────────

    def _trigger_jenkins(self, service, version, env, namespace,
                          platform, cluster):
        step(f"Triggering Jenkins deploy job (platform={platform})")
        url = (
            f"{self.cfg.jenkins_url}/job/platform-deploy/buildWithParameters"
            f"?SERVICE={service}&VERSION={version}&ENV={env}"
            f"&NAMESPACE={namespace}&PLATFORM={platform}&CLUSTER={cluster}"
        )
        resp = requests.post(
            url,
            auth=(self.cfg.jenkins_user, self.cfg.jenkins_token),
        )
        if resp.status_code not in (200, 201):
            warn(f"Jenkins returned {resp.status_code} — check the job manually.")

    # ─────────────────────────────────────────────────────────────────────────
    # PRIVATE — OpenShift direct deploy
    # ─────────────────────────────────────────────────────────────────────────

    def _deploy_openshift(self, profile: ClusterProfile, service, version,
                           namespace, wait, env):
        """Deploy to OpenShift using Helm.

        Auth: expects an active `oc login` session (context set in kubeconfig)
        or uses the cluster context declared in platform.yaml.
        """
        step(f"OpenShift deploy → {profile.name} / {namespace}")
        self._ensure_oc_context(profile)
        chart_dir = self._fetch_chart(service, version, env)
        self._helm_deploy(
            service=service,
            version=version,
            namespace=namespace,
            values_suffix=profile.helm_values_suffix,
            wait=wait,
            chart_dir=chart_dir,
            extra_args=["--set", f"openshift.enabled=true"],
        )

    def _ensure_oc_context(self, profile: ClusterProfile):
        """Switch to the correct oc context, or log a warning if unavailable."""
        if not profile.context:
            return
        kube = kubectl_executable()
        if not kube:
            warn("oc/kubectl not found — assuming kubeconfig is already set.")
            return
        try:
            run(
                [kube, "config", "use-context", profile.context],
                check=True, capture_output=True,
            )
            step(f"Switched kubeconfig context to: {profile.context}")
        except subprocess.CalledProcessError:
            warn(
                f"Could not switch to context '{profile.context}'. "
                "Proceeding with current context — verify it targets the right cluster."
            )

    # ─────────────────────────────────────────────────────────────────────────
    # PRIVATE — AWS / EKS direct deploy
    # ─────────────────────────────────────────────────────────────────────────

    def _deploy_aws(self, profile: ClusterProfile, service, version,
                     namespace, wait, env):
        """Deploy to EKS using Helm.

        Auth: updates kubeconfig via AWS CLI (requires aws CLI + valid credentials).
        Image pull: assumes ECR credentials are already configured (via IRSA or
        node instance profile) — no explicit docker login needed.
        """
        step(f"AWS EKS deploy → {profile.cluster_name} ({profile.region}) / {namespace}")
        self._ensure_eks_context(profile)
        chart_dir = self._fetch_chart(service, version, env)
        self._helm_deploy(
            service=service,
            version=version,
            namespace=namespace,
            values_suffix=profile.helm_values_suffix,
            wait=wait,
            chart_dir=chart_dir,
            extra_args=["--set", f"aws.region={profile.region}"],
        )

    def _ensure_eks_context(self, profile: ClusterProfile):
        """Update kubeconfig for the EKS cluster via AWS CLI."""
        if not profile.cluster_name or not profile.region:
            warn("EKS cluster_name or region not set in cluster profile — skipping kubeconfig update.")
            return
        try:
            run(
                ["aws", "eks", "update-kubeconfig",
                 "--region", profile.region,
                 "--name", profile.cluster_name],
                check=True, capture_output=True,
            )
            step(f"Updated kubeconfig for EKS cluster: {profile.cluster_name}")
        except FileNotFoundError:
            warn(
                "AWS CLI not found — cannot update kubeconfig automatically.\n"
                f"  Run manually: aws eks update-kubeconfig "
                f"--region {profile.region} --name {profile.cluster_name}"
            )
        except subprocess.CalledProcessError as e:
            warn(
                f"aws eks update-kubeconfig failed: "
                f"{e.stderr.decode() if e.stderr else str(e)}"
            )

    # ─────────────────────────────────────────────────────────────────────────
    # PRIVATE — Chart fetch from service repo
    # ─────────────────────────────────────────────────────────────────────────

    def _authenticated_url(self, repo_url: str) -> str:
        """Inject the platform GitHub/Gitea token into an HTTP(S) git URL.

        Leaves SSH URLs and URLs that already carry credentials unchanged.
        """
        token = self.cfg.github_token
        if not token or not repo_url.startswith("http"):
            return repo_url
        proto, rest = repo_url.split("://", 1)
        if "@" in rest:
            # Strip existing embedded credentials before re-injecting.
            rest = rest.split("@", 1)[1]
        return f"{proto}://{token}@{rest}"

    def _resolve_git_ref(self, auth_url: str, version: str) -> str | None:
        """Return the git tag name for *version*, or None to use the default branch.

        Probes the remote for ``v{version}`` first, then ``{version}``.
        Falls back to None (default branch / HEAD) with a warning when neither
        tag exists, so callers can still fetch the latest chart for a hotfix.
        """
        for candidate in (f"v{version}", version):
            try:
                out = run(
                    ["git", "ls-remote", "--tags", "--exit-code",
                     auth_url, f"refs/tags/{candidate}"],
                    capture_output=True, text=True,
                )
                if out.returncode == 0 and out.stdout.strip():
                    return candidate
            except FileNotFoundError:
                warn("git not found — cannot probe remote tags.")
                return None
            except Exception:
                pass
        warn(
            f"No git tag found for version '{version}' "
            f"(tried v{version} and {version}) — fetching default branch HEAD."
        )
        return None

    def _fetch_chart(self, service: str, version: str, env: str) -> Path:
        """Fetch helm/ from the service's git repo and persist it locally.

        Destination:
            envs/{env}/charts/{service}/{version}/helm/   ← Helm chart
            envs/{env}/charts/{service}/{version}/chart-source.yaml ← provenance

        The chart directory is committed as part of the deployment audit commit
        (the existing ``git add envs/`` in _git_commit covers it automatically),
        so the exact chart used for each deploy is permanently traceable.

        Idempotent: if the directory already exists the fetch is skipped.
        """
        dest_root = self.cfg.root / "envs" / env / "charts" / service / version
        helm_dest = dest_root / "helm"

        if helm_dest.exists():
            step(f"Chart already fetched: envs/{env}/charts/{service}/{version}/helm/")
            return helm_dest

        # ── Resolve repo URL ──────────────────────────────────────────────
        try:
            svc_data = self.cfg.load_service(service)
        except FileNotFoundError:
            error_exit(
                f"Service '{service}' not found in the platform catalog. "
                f"Register it first: platform.sh svc create {service} <owner>"
            )

        repo_url = svc_data.get("repo_url", "")
        if not repo_url:
            error_exit(
                f"Service '{service}' has no repo_url in its catalog entry — "
                "cannot fetch Helm chart."
            )

        auth_url = self._authenticated_url(repo_url)

        step(f"Fetching Helm chart for {service}:{version} from {repo_url}")

        # ── Resolve git ref (tag) ─────────────────────────────────────────
        ref = self._resolve_git_ref(auth_url, version)

        # ── Shallow clone into a temp dir ─────────────────────────────────
        tmp = tempfile.mkdtemp(prefix=f"ap3-chart-{service}-")
        try:
            clone_cmd = ["git", "clone", "--depth", "1"]
            if ref is not None:
                clone_cmd += ["--branch", ref]
            clone_cmd += [auth_url, tmp]

            try:
                run(clone_cmd, check=True, capture_output=True)
            except subprocess.CalledProcessError as e:
                stderr = e.stderr.decode(errors="replace").strip() if e.stderr else str(e)
                error_exit(
                    f"Could not clone {repo_url} "
                    f"(ref={ref or 'default branch'}): {stderr}"
                )

            src_helm = Path(tmp) / "helm"
            if not src_helm.is_dir():
                error_exit(
                    f"No helm/ directory found in {repo_url} "
                    f"at ref '{ref or 'HEAD'}'. "
                    "Ensure the service repo contains a helm/ chart directory."
                )

            # ── Copy chart to persistent location ────────────────────────
            dest_root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(str(src_helm), str(helm_dest))

            # ── Write provenance sidecar ──────────────────────────────────
            provenance = {
                "service":    service,
                "version":    version,
                "env":        env,
                "repo_url":   repo_url,
                "git_ref":    ref or "HEAD (default branch)",
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
            with open(dest_root / "chart-source.yaml", "w") as fh:
                yaml.dump(provenance, fh, default_flow_style=False)

            step(f"Chart stored: envs/{env}/charts/{service}/{version}/")
            return helm_dest

        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ─────────────────────────────────────────────────────────────────────────
    # PRIVATE — Helm (shared by both platforms)
    # ─────────────────────────────────────────────────────────────────────────

    def _helm_deploy(self, service, version, namespace, values_suffix,
                      wait, chart_dir: Path, extra_args=None):
        """
        Run `helm upgrade --install` for the service.

        Uses the chart from *chart_dir* (fetched by _fetch_chart).
        Applies ``values-{values_suffix}.yaml`` from the same directory if present.
        """
        helm = helm_executable()
        if not helm:
            warn(
                "'helm' not found — skipping actual deploy. "
                "Install Helm: https://helm.sh/docs/intro/install/"
            )
            return

        values_file = chart_dir / f"values-{values_suffix}.yaml"
        cmd = [
            helm, "upgrade", "--install", service, str(chart_dir),
            "--namespace", namespace,
            "--create-namespace",
            "--set", f"image.tag={version}",
            "--atomic",
            "--history-max", "5",
        ]
        # Apply per-environment values file if it exists
        if values_file.exists():
            cmd += ["--values", str(values_file)]
        else:
            warn(f"Values file not found: {values_file.name} — deploying with defaults only.")

        if extra_args:
            cmd += extra_args

        if wait:
            cmd += ["--wait", "--timeout", "5m"]

        step(f"helm upgrade --install {service} (namespace: {namespace})")
        try:
            run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            error_exit(f"Helm deploy failed: {e}")
