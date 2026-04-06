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
import subprocess
from datetime import datetime, timezone

import requests

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
            # Direct local deploy
            if profile.is_openshift:
                self._deploy_openshift(profile, service, version, namespace, wait)
            else:
                self._deploy_aws(profile, service, version, namespace, wait)

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
        print(f"  [dry-run] helm upgrade --install {service} ./{service}/helm")
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
                           namespace, wait):
        """Deploy to OpenShift using Helm.

        Auth: expects an active `oc login` session (context set in kubeconfig)
        or uses the cluster context declared in platform.yaml.
        """
        step(f"OpenShift deploy → {profile.name} / {namespace}")
        self._ensure_oc_context(profile)
        self._helm_deploy(
            service=service,
            version=version,
            namespace=namespace,
            values_suffix=profile.helm_values_suffix,
            wait=wait,
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
                     namespace, wait):
        """Deploy to EKS using Helm.

        Auth: updates kubeconfig via AWS CLI (requires aws CLI + valid credentials).
        Image pull: assumes ECR credentials are already configured (via IRSA or
        node instance profile) — no explicit docker login needed.
        """
        step(f"AWS EKS deploy → {profile.cluster_name} ({profile.region}) / {namespace}")
        self._ensure_eks_context(profile)
        self._helm_deploy(
            service=service,
            version=version,
            namespace=namespace,
            values_suffix=profile.helm_values_suffix,
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
    # PRIVATE — Helm (shared by both platforms)
    # ─────────────────────────────────────────────────────────────────────────

    def _helm_deploy(self, service, version, namespace, values_suffix,
                      wait, extra_args=None):
        """
        Run `helm upgrade --install` for the service.

        Looks for the Helm chart at `./{service}/helm/` relative to cwd.
        Applies `helm/values-{values_suffix}.yaml` from the same directory.
        """
        helm = helm_executable()
        if not helm:
            warn(
                "'helm' not found — skipping actual deploy. "
                "Install Helm: https://helm.sh/docs/intro/install/"
            )
            return

        helm_dir = f"./{service}/helm"
        values_file = f"{helm_dir}/values-{values_suffix}.yaml"
        cmd = [
            helm, "upgrade", "--install", service, helm_dir,
            "--namespace", namespace,
            "--create-namespace",
            "--set", f"image.tag={version}",
            "--atomic",
            "--history-max", "5",
        ]
        # Apply per-environment values file if it exists
        import os
        if os.path.exists(values_file):
            cmd += ["--values", values_file]
        else:
            warn(f"Values file not found: {values_file} — deploying with defaults only.")

        if extra_args:
            cmd += extra_args

        if wait:
            cmd += ["--wait", "--timeout", "5m"]

        step(f"helm upgrade --install {service} (namespace: {namespace})")
        try:
            run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            error_exit(f"Helm deploy failed: {e}")
