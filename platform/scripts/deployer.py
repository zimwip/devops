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
import subprocess
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
            manifest = self.cfg.load_env_manifest(env)
        except FileNotFoundError:
            error_exit(f"Environment '{env}' not found.")

        cluster_name   = manifest.get("cluster", self.cfg.default_cluster_dev)
        profile        = self.cfg.get_cluster_profile(cluster_name)
        registry       = manifest.get("registry") or profile.registry
        image          = f"{registry}/{service}:{version}"
        ns_pattern     = manifest.get("namespace_pattern", f"{env}-{{service}}")
        namespace      = ns_pattern.replace("{service}", service)

        # ── Identity + confirmation disclaimer ─────────────────────────────
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
            actions.append("Direct Helm deploy from OCI registry (helm upgrade --install)")
        actions.append(f"Update envs/{env}/{service}/version.yaml")

        if not self.dry_run and not self.json_output:
            confirm_with_actor(format_disclaimer(identity, actions), force=force)

        if self.dry_run:
            self._print_dry_run(profile, service, version, namespace, image)
            return

        # ── Deploy path ────────────────────────────────────────────────────
        if self.cfg.jenkins_token:
            self._trigger_jenkins(service, version, env, namespace,
                                  profile.platform, cluster_name)
        else:
            if profile.is_openshift:
                self._deploy_openshift(profile, service, version, namespace, wait, env)
            else:
                self._deploy_aws(profile, service, version, namespace, wait, env)

        # ── Update per-service version.yaml + commit ───────────────────────
        now = datetime.now(timezone.utc).isoformat()
        actor = (
            identity.display_name if identity.display_email == ""
            else f"{identity.display_name} <{identity.display_email}>"
        )
        helm_registry = os.environ.get("HELM_REGISTRY", "registry.internal")
        # Determine Helm chart repo based on version type
        is_release = bool(__import__("re").match(r"^\d+\.\d+\.\d+$", version))
        helm_repo = "helm-release" if is_release else "helm-local"
        chart_repo = f"oci://{helm_registry}/{helm_repo}/{service}"

        svc_version_data = {
            "service":      service,
            "chart_version": version,
            "image_tag":    version,
            "chart_repo":   chart_repo,
            "image":        image,
            "deployed_at":  now,
            "deployed_by":  actor,
            "health":       "deploying",
        }
        # Merge into existing file if present (preserves custom fields)
        try:
            existing = self.cfg.load_service_version(env, service)
            existing.update(svc_version_data)
            svc_version_data = existing
        except FileNotFoundError:
            pass
        self.cfg.save_service_version(env, service, svc_version_data)

        # Update envs.yaml metadata
        manifest["updated_at"] = now
        manifest["updated_by"] = actor
        manifest["commit"] = self._git_head_sha()
        self.cfg.save_env_manifest(env, manifest)

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
        """Declare a desired deployment (GitOps pull model).

        Writes a deploy-request file at envs/{env}/{service}/deploy-request.yaml.
        Jenkins picks this up on the next successful build when version == 'latest'
        and auto == True, then calls execute_deploy_request().
        """
        from identity import resolve_identity

        step(f"Requesting deployment: {service}@{version} → {env}")

        try:
            self.cfg.load_env_manifest(env)
        except FileNotFoundError:
            error_exit(f"Environment '{env}' not found.")

        identity = resolve_identity(self.cfg)
        actor = (
            identity.display_name
            if identity.display_email == ""
            else f"{identity.display_name} <{identity.display_email}>"
        )
        now = datetime.now(timezone.utc).isoformat()

        request_data = {
            "service":          service,
            "requested_version": version,
            "requested_at":     now,
            "requested_by":     actor,
            "auto":             version == "latest",
            "status":           "pending",
            "fulfilled_version": None,
            "fulfilled_at":     None,
        }
        request_path = self.cfg.env_path(env) / service / "deploy-request.yaml"
        request_path.parent.mkdir(parents=True, exist_ok=True)
        with open(request_path, "w") as fh:
            yaml.dump(request_data, fh, default_flow_style=False, allow_unicode=True)

        self._git_commit(f"deploy-request: {service}@{version} → {env}")
        success(f"Deployment request recorded: {service}@{version} in {env}")

    def cancel_deploy_request(self, env: str, service: str, force: bool = False):
        """Remove a pending deployment request."""
        step(f"Cancelling deployment request: {service} in {env}")

        request_path = self.cfg.env_path(env) / service / "deploy-request.yaml"
        if not request_path.exists():
            error_exit(f"No pending deployment request for '{service}' in '{env}'.")

        request_path.unlink()
        self._git_commit(f"deploy-cancel: {service} in {env}")
        success(f"Deployment request cancelled: {service} in {env}")

    def execute_deploy_request(self, env: str, service: str, version: str,
                               force: bool = False, wait: bool = False):
        """Execute a pending deployment request with a resolved version.

        Called by Jenkins after resolving 'latest' to the actual built tag.
        Runs the real deploy(), then marks the request as fulfilled.
        """
        self.deploy(env=env, service=service, version=version,
                    wait=wait, force=force)

        # Mark the request as fulfilled
        request_path = self.cfg.env_path(env) / service / "deploy-request.yaml"
        if request_path.exists():
            with open(request_path) as fh:
                request_data = yaml.safe_load(fh) or {}
            request_data["status"] = "fulfilled"
            request_data["fulfilled_version"] = version
            request_data["fulfilled_at"] = datetime.now(timezone.utc).isoformat()
            with open(request_path, "w") as fh:
                yaml.dump(request_data, fh, default_flow_style=False, allow_unicode=True)
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
        helm_registry = os.environ.get("HELM_REGISTRY", "registry.internal")
        is_release = bool(__import__("re").match(r"^\d+\.\d+\.\d+$", version))
        helm_repo = "helm-release" if is_release else "helm-local"
        print(f"  [dry-run] platform   : {profile.platform}")
        print(f"  [dry-run] cluster    : {profile.name}")
        print(f"  [dry-run] image      : {image}")
        print(f"  [dry-run] namespace  : {namespace}")
        if profile.is_openshift:
            print(f"  [dry-run] oc login --server={profile.api_url}")
        else:
            print(f"  [dry-run] aws eks update-kubeconfig "
                  f"--region {profile.region} --name {profile.cluster_name}")
        print(f"  [dry-run] helm upgrade --install {service} "
              f"oci://{helm_registry}/{helm_repo}/{service}")
        print(f"  [dry-run]   --version {version} --namespace {namespace}")
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
        """Deploy to OpenShift using Helm chart from OCI registry."""
        step(f"OpenShift deploy → {profile.name} / {namespace}")
        self._ensure_oc_context(profile)
        self._helm_deploy_oci(
            service=service,
            version=version,
            namespace=namespace,
            env=env,
            wait=wait,
            extra_args=["--set", "openshift.enabled=true"],
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
        """Deploy to EKS using Helm chart from OCI registry."""
        step(f"AWS EKS deploy → {profile.cluster_name} ({profile.region}) / {namespace}")
        self._ensure_eks_context(profile)
        self._helm_deploy_oci(
            service=service,
            version=version,
            namespace=namespace,
            env=env,
            wait=wait,
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
    # PRIVATE — Helm OCI deploy
    # ─────────────────────────────────────────────────────────────────────────

    def _helm_deploy_oci(self, service: str, version: str, namespace: str,
                          env: str, wait: bool, extra_args=None):
        """Run `helm upgrade --install` pulling the chart from the OCI registry.

        Release versions (X.Y.Z) are pulled from helm-release (immutable).
        Everything else (SNAPSHOT, rc, poc) is pulled from helm-local.

        Per-service values.yaml from the platform-config repo is applied as an
        additional --values override when present.
        """
        helm = helm_executable()
        if not helm:
            warn(
                "'helm' not found — skipping actual deploy. "
                "Install Helm: https://helm.sh/docs/intro/install/"
            )
            return

        import re as _re
        is_release = bool(_re.match(r"^\d+\.\d+\.\d+$", version))
        helm_registry = os.environ.get("HELM_REGISTRY", "registry.internal")
        helm_repo = "helm-release" if is_release else "helm-local"
        chart_ref = f"oci://{helm_registry}/{helm_repo}/{service}"

        cmd = [
            helm, "upgrade", "--install", service, chart_ref,
            "--version", version,
            "--namespace", namespace,
            "--create-namespace",
            "--set", f"image.tag={version}",
            "--atomic",
            "--history-max", "5",
        ]

        # Apply per-service values.yaml from platform-config if present
        values_path = self.cfg.service_values_path(env, service)
        if values_path.exists():
            cmd += ["--values", str(values_path)]
        else:
            step(f"No values.yaml for {service} in {env} — deploying with chart defaults")

        if extra_args:
            cmd += extra_args

        if wait:
            cmd += ["--wait", "--timeout", "5m"]

        step(f"helm upgrade --install {service} from {chart_ref}:{version} (ns: {namespace})")
        try:
            run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            error_exit(f"Helm deploy failed: {e}")
