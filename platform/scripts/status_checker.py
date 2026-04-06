"""
status_checker.py — Live environment status check.

Compares the expected state in envs/*/versions.yaml against the actual
running state on the target cluster.

Token resolution (tried in order, per cluster):
  1. {CLUSTER_NAME_UPPER}_TOKEN  e.g. OPENSHIFT_PROD_TOKEN for "openshift-prod"
  2. OC_TOKEN                    OpenShift global fallback
  3. KUBE_TOKEN                  generic Kubernetes fallback

Cluster access strategy:
  - OpenShift / K8s REST API  when api_url + token are available (no CLI needed)
  - kubectl fallback           when a kubeconfig context is configured but no token
  - "unreachable"              when neither is available
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

from config import PlatformConfig


# ── Data model ────────────────────────────────────────────────────────────────

STATUS_OK       = "ok"        # expected version running, all pods ready
STATUS_DEGRADED = "degraded"  # expected version running, some pods not ready
STATUS_DRIFT    = "drift"     # different version running than expected
STATUS_MISSING  = "missing"   # no deployment found on cluster
STATUS_UNKNOWN  = "unknown"   # cluster unreachable or check skipped


@dataclass
class ServiceLiveStatus:
    name: str
    expected_version: str
    expected_image: str
    running_image: Optional[str] = None
    running_version: Optional[str] = None   # tag extracted from running image
    ready_replicas: int = 0
    desired_replicas: int = 0
    status: str = STATUS_UNKNOWN
    message: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class EnvLiveStatus:
    name: str
    cluster: str
    namespace: str
    platform: str
    reachable: bool
    services: list[ServiceLiveStatus] = field(default_factory=list)
    checked_at: str = ""
    error: Optional[str] = None

    @property
    def overall(self) -> str:
        if not self.reachable:
            return STATUS_UNKNOWN
        statuses = {s.status for s in self.services}
        for worst in (STATUS_DRIFT, STATUS_MISSING, STATUS_DEGRADED, STATUS_UNKNOWN):
            if worst in statuses:
                return worst
        if statuses == {STATUS_OK}:
            return STATUS_OK
        return STATUS_UNKNOWN

    def as_dict(self) -> dict:
        d = asdict(self)
        d["overall"] = self.overall
        return d


# ── Checker ───────────────────────────────────────────────────────────────────

class StatusChecker:
    def __init__(self, cfg: PlatformConfig):
        self.cfg = cfg
        # Suppress InsecureRequestWarning for self-signed internal cluster certs
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass

    def check_all(self) -> list[EnvLiveStatus]:
        return [self.check_env(name) for name in self.cfg.list_envs()]

    def check_env(self, env_name: str) -> EnvLiveStatus:
        try:
            data = self.cfg.load_versions(env_name)
        except FileNotFoundError:
            return EnvLiveStatus(
                name=env_name, cluster="", namespace="", platform="openshift",
                reachable=False, checked_at=_now(),
                error=f"Environment '{env_name}' not found",
            )

        meta      = data.get("_meta", {})
        cluster   = meta.get("cluster", self.cfg.default_cluster_dev)
        namespace = meta.get("namespace", f"platform-{env_name}")
        profile   = self.cfg.get_cluster_profile(cluster)

        # Fetch live deployments from the cluster
        live_deployments, reachable, error = self._fetch_deployments(profile, namespace)

        services: list[ServiceLiveStatus] = []
        for svc_name, svc_data in (data.get("services") or {}).items():
            expected_image   = svc_data.get("image", "")
            expected_version = svc_data.get("version", "")
            svc_status = self._compare(
                svc_name, expected_version, expected_image,
                live_deployments, reachable,
            )
            services.append(svc_status)

        return EnvLiveStatus(
            name=env_name,
            cluster=cluster,
            namespace=namespace,
            platform=profile.platform,
            reachable=reachable,
            services=services,
            checked_at=_now(),
            error=error,
        )

    # ── Comparison ────────────────────────────────────────────────────────────

    def _compare(
        self,
        name: str,
        expected_version: str,
        expected_image: str,
        live: dict,
        reachable: bool,
    ) -> ServiceLiveStatus:
        if not reachable:
            return ServiceLiveStatus(
                name=name, expected_version=expected_version,
                expected_image=expected_image, status=STATUS_UNKNOWN,
                message="Cluster unreachable",
            )

        dep = live.get(name)
        if dep is None:
            return ServiceLiveStatus(
                name=name, expected_version=expected_version,
                expected_image=expected_image, status=STATUS_MISSING,
                message="No deployment found on cluster",
            )

        running_image   = dep["image"]
        ready           = dep["ready_replicas"]
        desired         = dep["desired_replicas"]
        running_version = _extract_tag(running_image)

        if running_image and expected_image and running_image != expected_image:
            status = STATUS_DRIFT
            message = f"Running {running_version or running_image!r}, expected {expected_version!r}"
        elif ready < desired:
            status = STATUS_DEGRADED
            message = f"{ready}/{desired} pods ready"
        else:
            status = STATUS_OK
            message = f"{ready}/{desired} pods ready"

        return ServiceLiveStatus(
            name=name,
            expected_version=expected_version,
            expected_image=expected_image,
            running_image=running_image,
            running_version=running_version,
            ready_replicas=ready,
            desired_replicas=desired,
            status=status,
            message=message,
        )

    # ── Cluster access ────────────────────────────────────────────────────────

    def _fetch_deployments(
        self, profile, namespace: str
    ) -> tuple[dict, bool, Optional[str]]:
        """
        Returns (deployments_dict, reachable, error_message).
        deployments_dict: {service_name: {image, ready_replicas, desired_replicas}}
        """
        token = self._resolve_token(profile)

        # Strategy 1: REST API with bearer token
        if token and profile.api_url:
            result, err = self._fetch_via_api(profile.api_url, namespace, token)
            if result is not None:
                return result, True, None
            # Fall through to kubectl if API failed
            error_hint = err
        else:
            error_hint = None

        # Strategy 2: kubectl with kubeconfig context
        if profile.context:
            result, err = self._fetch_via_kubectl(profile.context, namespace)
            if result is not None:
                return result, True, None
            return {}, False, err or error_hint

        msg = (
            error_hint
            or f"No token ({self._token_env_name(profile)} not set) and no kubeconfig context configured"
        )
        return {}, False, msg

    def _fetch_via_api(
        self, api_url: str, namespace: str, token: str
    ) -> tuple[Optional[dict], Optional[str]]:
        try:
            import requests as _req
            url = f"{api_url.rstrip('/')}/apis/apps/v1/namespaces/{namespace}/deployments"
            resp = _req.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                verify=False,
                timeout=10,
            )
            if resp.status_code == 401:
                return None, "Token rejected (401 Unauthorized)"
            if resp.status_code == 403:
                return None, "Token has no read access to deployments (403 Forbidden)"
            if resp.status_code == 404:
                return None, f"Namespace '{namespace}' not found (404)"
            if resp.status_code != 200:
                return None, f"Cluster API returned HTTP {resp.status_code}"
            return _parse_deployment_list(resp.json()), None
        except Exception as e:
            return None, f"API request failed: {e}"

    def _fetch_via_kubectl(
        self, context: str, namespace: str
    ) -> tuple[Optional[dict], Optional[str]]:
        try:
            result = subprocess.run(
                ["kubectl", "get", "deployments",
                 "-n", namespace, "--context", context, "-o", "json"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                err = result.stderr.strip()
                return None, f"kubectl failed: {err[:200]}"
            return _parse_deployment_list(json.loads(result.stdout)), None
        except FileNotFoundError:
            return None, "kubectl not found in PATH"
        except Exception as e:
            return None, f"kubectl failed: {e}"

    # ── Token helpers ─────────────────────────────────────────────────────────

    def _token_env_name(self, profile) -> str:
        """Derive the primary env-var name for a cluster's token."""
        return profile.name.upper().replace("-", "_") + "_TOKEN"

    def _resolve_token(self, profile) -> Optional[str]:
        # 1. Per-cluster: e.g. OPENSHIFT_PROD_TOKEN
        token = os.environ.get(self._token_env_name(profile))
        if token:
            return token
        # 2. Platform-type fallback
        if profile.is_openshift:
            return os.environ.get("OC_TOKEN")
        return os.environ.get("KUBE_TOKEN")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_deployment_list(body: dict) -> dict:
    """
    Turn a Kubernetes DeploymentList API response into
    {deployment_name: {image, ready_replicas, desired_replicas}}.
    """
    result = {}
    for dep in body.get("items", []):
        name = dep.get("metadata", {}).get("name", "")
        containers = dep.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        image = containers[0].get("image", "") if containers else ""
        status = dep.get("status", {})
        ready   = status.get("readyReplicas") or 0
        desired = dep.get("spec", {}).get("replicas", 1)
        result[name] = {
            "image":           image,
            "ready_replicas":  ready,
            "desired_replicas": desired,
        }
    return result


def _extract_tag(image: str) -> Optional[str]:
    """Extract the image tag from a full image reference."""
    if not image:
        return None
    # registry/name:tag  or  registry/name@sha256:...
    if "@" in image:
        return image.split("@", 1)[1][:16]  # show digest prefix
    if ":" in image:
        return image.rsplit(":", 1)[1]
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── CLI formatter ─────────────────────────────────────────────────────────────

STATUS_ICONS = {
    STATUS_OK:       "+",
    STATUS_DEGRADED: "~",
    STATUS_DRIFT:    "!",
    STATUS_MISSING:  "x",
    STATUS_UNKNOWN:  "?",
}

STATUS_LABELS = {
    STATUS_OK:       "healthy",
    STATUS_DEGRADED: "degraded — some pods not ready",
    STATUS_DRIFT:    "version drift — wrong version running",
    STATUS_MISSING:  "not deployed — no pods found on cluster",
    STATUS_UNKNOWN:  "unknown — cluster unreachable or check skipped",
}


def format_status_table(results: list[EnvLiveStatus]) -> str:
    lines = []
    for env in results:
        reach = "reachable" if env.reachable else f"UNREACHABLE — {env.error or ''}"
        overall_label = STATUS_LABELS.get(env.overall, env.overall).upper().split(" —")[0]
        lines.append(f"\n  {env.name}  [{env.platform} / {env.cluster} / ns:{env.namespace}]")
        lines.append(f"  Status: {overall_label}  |  {reach}")
        lines.append(f"  Checked: {env.checked_at}")
        if not env.services:
            lines.append("  (no services)")
            continue
        col_w = [26, 14, 14, 22, 6]
        header = ["Service", "Expected", "Running", "Status", "Pods"]
        def fmt(row):
            return "  " + "  ".join(str(v)[:w].ljust(w) for v, w in zip(row, col_w))
        lines.append(fmt(header))
        lines.append("  " + "  ".join("-" * w for w in col_w))
        for s in env.services:
            icon = STATUS_ICONS.get(s.status, "?")
            label = STATUS_LABELS.get(s.status, s.status).split(" —")[0]
            pods = f"{s.ready_replicas}/{s.desired_replicas}" if s.status not in (STATUS_MISSING, STATUS_UNKNOWN) else "—"
            lines.append(fmt([
                s.name,
                s.expected_version or "—",
                s.running_version or ("—" if s.status in (STATUS_MISSING, STATUS_UNKNOWN) else s.running_image or "?"),
                f"{icon} {label}",
                pods,
            ]))
    lines.append("")
    return "\n".join(lines)
