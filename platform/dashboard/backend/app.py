#!/usr/bin/env python3
"""
dashboard/backend/app.py
FastAPI server powering the platform dashboard.

Read operations (list/get) query the platform state directly from envs/*.yaml.
Write operations (create, remove, deploy) delegate to platform_cli.py via
subprocess so the CLI remains the single authority for all mutations.

Run (dev, with hot-reload):
    uvicorn app:app --reload --port 5173

Run (direct):
    python dashboard/backend/app.py

Auto-generated API docs:
    http://localhost:5173/docs      <- Swagger UI
    http://localhost:5173/redoc     <- ReDoc
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import requests as _requests

# Allow imports from scripts/
SCRIPTS_DIR = Path(__file__).parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from config import PlatformConfig
from env_manager import EnvManager
from identity import resolve_identity, ActorIdentity
from history import HistoryCollector
from cluster_manager import ClusterManager


CLI = [sys.executable, str(SCRIPTS_DIR / "platform_cli.py")]


def _run_cli(*args: str) -> dict:
    """Run platform_cli.py with --json and return parsed output.
    Raises HTTPException on non-zero exit, surfacing the CLI error message."""
    result = subprocess.run(
        [*CLI, "--json", *args],
        capture_output=True,
        text=True,
    )
    # Try to parse stdout as JSON regardless of exit code —
    # on failure the CLI emits {"error": "..."} to stdout in --json mode.
    parsed: dict = {}
    if result.stdout.strip():
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError:
            pass

    if result.returncode != 0:
        msg = (
            result.stderr.strip()         # error_exit() writes here; most specific
            or parsed.get("error")        # structured error from Exception handler
            or result.stdout.strip()
            or "CLI command failed"
        )
        raise HTTPException(status_code=500, detail=msg)

    return parsed

# ── App ───────────────────────────────────────────────────────────────────────

FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"

app = FastAPI(
    title="Platform Dashboard API",
    description=(
        "REST API for the AP3 platform. "
        "Manages services, environments and deployments. "
        "Mirrors every action available in the `platform.py` CLI."
    ),
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

cfg = PlatformConfig()


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class ServiceVersionDetail(BaseModel):
    version: str
    image: str | None = None
    deployed_at: str | None = None
    health: str = "unknown"
    experimental: bool = False


class EnvSummary(BaseModel):
    name: str
    type: str = "fixed"
    platform: str = "openshift"
    cluster: str = "unknown"
    namespace: str = "unknown"
    owner: str = "unknown"
    description: str = ""
    expires_at: str | None = None
    updated_at: str | None = None
    services: dict[str, ServiceVersionDetail] = {}
    requested_deployments: dict[str, dict] = {}
    error: str | None = None
    # Expiry status — only populated for POC environments
    expiry_status: str | None = None   # "ok" | "warning" | "expired" | None
    days_remaining: int | None = None  # negative when expired


class ServiceSummary(BaseModel):
    name: str
    versions: dict[str, ServiceVersionDetail] = {}
    last_deployed: str | None = None
    repo_url: str = ""
    repo_exists: bool | None = None    # None = not checked / unknown
    repo_warning: str = ""
    gitflow_ok: bool | None = None     # None = not checked
    gitflow_missing: list[str] = []    # branch names that are absent
    ap3_hosted: bool | None = None     # False = external repo
    jenkins_ok: bool | None = None     # None = not checked
    jenkins_warning: str = ""


class CreateServiceRequest(BaseModel):
    name: str = Field(..., description="Service name in kebab-case",
                      json_schema_extra={"example": "my-service"})
    owner: str = Field(..., description="Owning team",
                       json_schema_extra={"example": "team-backend"})
    description: str = Field("", description="Short service description")

    # ── Source mode ────────────────────────────────────────────────────────
    # Exactly one of these three determines what AP3 does with the repo.
    source_mode: str = Field(
        "template",
        description="How to create the service repo: "
                    "'template' = scaffold from a built-in template (AP3-hosted); "
                    "'fork' = fork an existing AP3-hosted service; "
                    "'external' = reference an existing repo by URL (not AP3-hosted).",
    )
    # template mode
    template: str = Field(
        "springboot",
        description="[template mode] Scaffold template. "
                    "Choices: springboot | react | python-api",
    )
    # fork mode
    fork_from: str = Field(
        "",
        description="[fork mode] Name of an existing AP3-hosted service to fork from.",
        json_schema_extra={"example": "service-auth"},
    )
    # external mode
    external_repo_url: str = Field(
        "",
        description="[external mode] Full Git/GitHub URL of the existing repo.",
        json_schema_extra={"example": "https://github.com/my-org/existing-service.git"},
    )

    skip_jenkins: bool = Field(False, description="Skip Jenkins pipeline registration")
    force: bool = Field(True, description="Skip CLI confirmation prompt — UI shows its own confirmation step")

    @field_validator("source_mode")
    @classmethod
    def validate_source_mode(cls, v):
        if v not in ("template", "fork", "external"):
            raise ValueError("source_mode must be 'template', 'fork', or 'external'")
        return v


class CreateServiceResponse(BaseModel):
    status: str
    name: str
    warnings: list[str] = []
    steps: list[dict] = []


class CreateEnvResponse(BaseModel):
    """Returned on successful POC environment creation.
    The 'warnings' list contains non-fatal issues (missing tokens, skipped steps,
    git commit failures). Status is always 'created' even when warnings are present —
    the environment YAML was written. Warnings require manual follow-up.
    """
    status: str = "created"
    name: str
    warnings: list[str] = []


class CreateEnvRequest(BaseModel):
    name: str = Field(..., description="Short POC name — auto-prefixed with poc-", json_schema_extra={"example": "payment-experiment"})
    base: str = Field("staging", description="Base environment to fork versions from")
    platform: str | None = Field(
        None,
        description="Target platform: 'openshift' or 'aws'. Derived from cluster profile when omitted.",
        json_schema_extra={"example": "openshift"},
    )
    cluster: str | None = Field(
        None,
        description="Target cluster name as defined in platform.yaml. Defaults to base env's cluster.",
        json_schema_extra={"example": "openshift-dev"},
    )
    namespace: str | None = Field(
        None,
        description=(
            "Pre-existing namespace to use. "
            "Required when you have no rights to create namespaces. "
            "Defaults to auto-generated 'platform-{env-name}'."
        ),
        json_schema_extra={"example": "my-team-poc-ns"},
    )
    owner: str = Field("dashboard-user", description="POC owner")
    description: str = Field("", description="POC purpose")
    ttl_days: int = Field(14, description="Time-to-live in days", ge=1, le=365)
    force: bool = Field(False, description="Skip confirmation — always True from the dashboard (confirmation shown in UI)")

    @field_validator("platform")
    @classmethod
    def validate_platform(cls, v):
        if v is not None and v not in ("openshift", "aws"):
            raise ValueError("platform must be 'openshift' or 'aws'")
        return v


class DeployRequest(BaseModel):
    env: str = Field(..., description="Target environment name", json_schema_extra={"example": "dev"})
    service: str = Field(..., description="Service name", json_schema_extra={"example": "service-auth"})
    version: str = Field(..., description="Version to deploy (semver)", json_schema_extra={"example": "2.3.0"})
    wait: bool = Field(False, description="Wait for rollout to complete")
    platform: str | None = Field(
        None,
        description="Override target platform: 'openshift' or 'aws'. "
                    "Normally derived from the environment's cluster profile.",
        json_schema_extra={"example": "aws"},
    )
    force: bool = Field(True, description="Skip CLI confirmation prompt — UI shows its own confirmation step")

    @field_validator("platform")
    @classmethod
    def validate_platform(cls, v):
        if v is not None and v not in ("openshift", "aws"):
            raise ValueError("platform must be 'openshift' or 'aws'")
        return v


class DeployResponse(BaseModel):
    status: str
    env: str
    service: str
    version: str


class DeployRequestV2(BaseModel):
    service: str = Field(..., description="Service name", json_schema_extra={"example": "service-auth"})
    version: str = Field(..., description="Semver version or 'latest'", json_schema_extra={"example": "latest"})


class DeployRequestV2Response(BaseModel):
    status: str
    env: str
    service: str
    requested_version: str
    auto: bool
    requested_at: str


class DeployRequestStatus(BaseModel):
    service: str
    requested_version: str
    requested_at: str
    requested_by: str
    auto: bool
    status: str
    fulfilled_version: str | None = None
    fulfilled_at: str | None = None


class TemplateInfo(BaseModel):
    id: str
    description: str
    language: str = ""
    created_at: str | None = None
    created_by: str | None = None


class AddTemplateRequest(BaseModel):
    name: str = Field(..., description="Template name (kebab-case)",
                      json_schema_extra={"example": "quarkus"})
    from_dir: str = Field(..., description="Absolute path to the source directory on the server",
                          json_schema_extra={"example": "/opt/templates/quarkus"})
    description: str = Field("", description="Short description")
    language: str = Field("", description="Primary language (e.g. java, python, javascript)")
    force: bool = Field(False, description="Overwrite if template already exists")


class DestroyResponse(BaseModel):
    status: str
    name: str


class GitHubRepoInfo(BaseModel):
    name: str
    clone_url: str
    description: str = ""
    language: str = ""
    updated_at: str | None = None
    private: bool = False


class RemoveServiceResponse(BaseModel):
    status: str
    name: str
    envs: list[str] = []
    warnings: list[str] = []
    steps: list[dict] = []


class ExtendEnvRequest(BaseModel):
    ttl_days: int = Field(14, description="Additional days to add to the TTL (max total: 365 days from today)", ge=1, le=365)


class ExtendEnvResponse(BaseModel):
    status: str
    name: str
    expires_at: str
    days_remaining: int


class IdentityResponse(BaseModel):
    github_login: str | None = None
    github_name: str | None = None
    github_email: str | None = None
    jenkins_user: str | None = None
    jenkins_url: str | None = None
    git_name: str | None = None
    git_email: str | None = None
    display_name: str
    display_email: str
    warnings: list[str] = []


class CommitInfo(BaseModel):
    sha: str
    short_sha: str
    message: str        # first line of the commit message
    author: str
    date: str           # ISO-8601
    tags: list[str] = []


class ReleaseInfo(BaseModel):
    tag: str
    name: str
    date: str
    notes: str = ""


class ServiceDeployEvent(BaseModel):
    env: str
    version: str
    deployed_at: str
    actor: str


class ServiceHistoryResponse(BaseModel):
    commits: list[CommitInfo] = []
    releases: list[ReleaseInfo] = []
    deployments: list[ServiceDeployEvent] = []
    github_available: bool = True
    repo_url: str = ""


class ServiceLiveStatusSchema(BaseModel):
    name: str
    expected_version: str
    expected_image: str
    running_image: str | None = None
    running_version: str | None = None
    ready_replicas: int = 0
    desired_replicas: int = 0
    status: str
    message: str = ""


class EnvLiveStatusSchema(BaseModel):
    name: str
    cluster: str
    namespace: str
    platform: str
    reachable: bool
    overall: str
    services: list[ServiceLiveStatusSchema] = []
    checked_at: str = ""
    error: str | None = None


class AuditEventSchema(BaseModel):
    timestamp: str
    event_type: str
    label: str
    actor: str
    env: str
    service: str | None = None
    version: str | None = None
    image: str | None = None
    commit: str | None = None
    message: str | None = None
    platform: str | None = None
    cluster: str | None = None


class PlatformConfigSchema(BaseModel):
    """Editable platform-level settings from platform.yaml."""
    github_url: str = "https://github.com"
    github_account_type: str = "org"
    github_org: str = "my-org"
    jenkins_url: str = "https://jenkins.internal"
    # Token presence (never return actual values)
    github_token_set: bool = False
    jenkins_token_set: bool = False
    jenkins_user_set: bool = False


class UpdatePlatformConfigRequest(BaseModel):
    github_url: str | None = None
    github_account_type: str | None = None
    github_org: str | None = None
    jenkins_url: str | None = None

    @field_validator("github_account_type")
    @classmethod
    def validate_account_type(cls, v):
        if v is not None and v not in ("org", "user"):
            raise ValueError("github_account_type must be 'org' or 'user'")
        return v


class ClusterSchema(BaseModel):
    name: str
    platform: str
    registry: str
    helm_values_suffix: str
    in_use: list[str] = []
    # OpenShift-specific
    api_url: str | None = None
    context: str | None = None
    # AWS-specific
    region: str | None = None
    cluster_name: str | None = None


# ── Live pod / cluster schemas ────────────────────────────────────────────────

class PodContainerSchema(BaseModel):
    name: str
    image: str
    ready: bool
    restart_count: int
    state: str


class PodInfoSchema(BaseModel):
    name: str
    namespace: str
    phase: str
    ready: str
    restarts: int
    age: str
    node: str
    containers: list[PodContainerSchema] = []


class EnvPodsSchema(BaseModel):
    env_name: str
    cluster: str
    namespace: str
    reachable: bool
    pods: list[PodInfoSchema] = []
    checked_at: str = ""
    error: str | None = None


class ClusterNodeSchema(BaseModel):
    name: str
    status: str
    roles: str
    version: str
    os: str


class ClusterNamespaceSchema(BaseModel):
    name: str
    pod_count: int
    running: int
    pending: int
    failed: int
    pods: list[PodInfoSchema] = []


class ClusterLiveSchema(BaseModel):
    cluster: str
    reachable: bool
    checked_at: str
    nodes: list[ClusterNodeSchema] = []
    namespaces: list[ClusterNamespaceSchema] = []
    error: str | None = None


class AddClusterRequest(BaseModel):
    name: str = Field(..., description="Cluster name (e.g. openshift-dev, eks-prod)",
                      json_schema_extra={"example": "openshift-dev"})
    platform: str = Field(..., description="Platform type: 'openshift' or 'aws'",
                          json_schema_extra={"example": "openshift"})
    # OpenShift
    api_url: str = Field("", description="[OpenShift] API server URL",
                         json_schema_extra={"example": "https://api.cluster.example.com:6443"})
    context: str = Field("", description="[OpenShift] kubeconfig context name")
    # AWS
    region: str = Field("", description="[AWS] AWS region",
                        json_schema_extra={"example": "eu-west-1"})
    cluster_name: str = Field("", description="[AWS] EKS cluster name")
    # Shared
    registry: str = Field("", description="Container registry URL (defaults to platform registry)")
    helm_values_suffix: str = Field("", description="Helm values suffix (defaults to last segment of cluster name)")

    @field_validator("platform")
    @classmethod
    def validate_platform(cls, v):
        if v not in ("openshift", "aws"):
            raise ValueError("platform must be 'openshift' or 'aws'")
        return v


# ── Internal helpers ──────────────────────────────────────────────────────────

def _build_env_summary(env_name: str) -> EnvSummary:
    try:
        data = cfg.load_versions(env_name)
    except FileNotFoundError:
        return EnvSummary(name=env_name, error="versions.yaml not found")

    meta = data.get("_meta", {})
    services = {
        svc: ServiceVersionDetail(
            version=d.get("version", "unknown"),
            image=d.get("image"),
            deployed_at=d.get("deployed_at"),
            health=d.get("health", "unknown"),
            experimental=d.get("experimental", False),
        )
        for svc, d in (data.get("services") or {}).items()
    }
    from env_manager import EnvManager as _EM
    expiry = _EM(cfg)._expiry_status(data)

    return EnvSummary(
        name=env_name,
        type=meta.get("env_type", "fixed"),
        platform=meta.get("platform", "openshift"),
        cluster=meta.get("cluster", "unknown"),
        namespace=meta.get("namespace", "unknown"),
        owner=meta.get("owner", meta.get("updated_by", "unknown")),
        description=meta.get("description", ""),
        expires_at=meta.get("expires_at"),
        updated_at=meta.get("updated_at"),
        services=services,
        requested_deployments=data.get("requested_deployments") or {},
        expiry_status=expiry.get("status"),
        days_remaining=expiry.get("days_remaining"),
    )


def _collect_all_services() -> list[ServiceSummary]:
    service_map: dict[str, ServiceSummary] = {}

    # Seed from service catalog so catalog-only services appear even if not deployed
    for svc_name in cfg.list_service_names():
        try:
            catalog = cfg.load_service(svc_name)
        except Exception:
            catalog = {}
        service_map[svc_name] = ServiceSummary(
            name=svc_name,
            repo_url=catalog.get("repo_url", ""),
            ap3_hosted=catalog.get("ap3_hosted"),  # None if catalog entry predates this field
        )

    for env_name in cfg.list_envs():
        try:
            data = cfg.load_versions(env_name)
        except Exception:
            continue
        for svc, d in (data.get("services") or {}).items():
            if svc not in service_map:
                service_map[svc] = ServiceSummary(name=svc)
            service_map[svc].versions[env_name] = ServiceVersionDetail(
                version=d.get("version", "unknown"),
                deployed_at=d.get("deployed_at"),
                health=d.get("health", "unknown"),
            )
            if d.get("deployed_at"):
                service_map[svc].last_deployed = d["deployed_at"]
    return sorted(service_map.values(), key=lambda s: s.name)


def _check_repo_exists(repo_url: str) -> tuple[bool | None, str]:
    """Check whether repo_url is reachable. Returns (exists, warning_message)."""
    import re as _re
    if not repo_url:
        return None, ""

    url_match = _re.search(r"https?://([^/]+)/([^/]+)/([^/]+?)(?:\.git)?$", repo_url)
    if url_match and cfg.github_token:
        host_part, owner, repo = url_match.group(1), url_match.group(2), url_match.group(3)
        host_url = f"http{'s' if repo_url.startswith('https') else ''}://{host_part}"
        # Use cfg.github_api_base when the host matches the configured git server
        # (handles Gitea /api/v1, GHE /api/v3, github.com api.github.com)
        cfg_host = _re.sub(r"^https?://", "", cfg.github_url.rstrip("/"))
        if host_part == cfg_host:
            api_base = cfg.github_api_base
        elif "github.com" == host_part:
            api_base = "https://api.github.com"
        else:
            api_base = f"{host_url}/api/v3"
        try:
            resp = _requests.get(
                f"{api_base}/repos/{owner}/{repo}",
                headers={
                    "Authorization": f"token {cfg.github_token}",
                    "Accept": "application/vnd.github.v3+json",
                },
                timeout=8,
            )
            if resp.status_code == 200:
                return True, ""
            if resp.status_code == 404:
                return False, f"Repository not found: {repo_url}"
            if resp.status_code in (401, 403):
                return None, f"Repository access denied (HTTP {resp.status_code}) — check GITHUB_TOKEN"
            return None, f"Git hosting API returned HTTP {resp.status_code}"
        except Exception as exc:
            return None, f"Repository check failed: {exc}"

    # Fallback: plain HEAD (public repos, no token)
    host_match = _re.match(r"(https?://[^/]+)", repo_url)
    is_public_gh = host_match and "github.com" in host_match.group(1)
    try:
        resp = _requests.head(repo_url.rstrip("/"), timeout=8,
                              allow_redirects=True, verify=bool(is_public_gh))
        if resp.status_code == 200:
            return True, ""
        if resp.status_code == 404:
            return False, f"Repository not found: {repo_url}"
        if resp.status_code in (401, 403):
            return None, f"Repository access denied (HTTP {resp.status_code})"
        return None, f"Repository returned HTTP {resp.status_code}"
    except Exception as exc:
        return None, f"Repository check failed: {exc}"


_GITFLOW_BRANCHES = ("main", "develop")


def _check_gitflow(repo_url: str) -> tuple[bool | None, list[str]]:
    """Check that the repo has all required GitFlow branches.

    Returns (ok, missing_branches).
    ok=None means the check could not be performed (no token / repo unreachable).
    """
    import re as _re
    if not repo_url or not cfg.github_token:
        return None, []

    url_match = _re.search(r"https?://([^/]+)/([^/]+)/([^/]+?)(?:\.git)?$", repo_url)
    if not url_match:
        return None, []

    host_part, owner, repo = url_match.group(1), url_match.group(2), url_match.group(3)
    cfg_host = _re.sub(r"^https?://", "", cfg.github_url.rstrip("/"))
    if host_part == cfg_host:
        api_base = cfg.github_api_base
    elif "github.com" == host_part:
        api_base = "https://api.github.com"
    else:
        host_url = f"http{'s' if repo_url.startswith('https') else ''}://{host_part}"
        api_base = f"{host_url}/api/v3"

    try:
        resp = _requests.get(
            f"{api_base}/repos/{owner}/{repo}/branches",
            headers={
                "Authorization": f"token {cfg.github_token}",
                "Accept": "application/vnd.github.v3+json",
            },
            timeout=8,
        )
        if resp.status_code != 200:
            return None, []
        existing = {b["name"] for b in resp.json()}
        missing = [b for b in _GITFLOW_BRANCHES if b not in existing]
        return (len(missing) == 0), missing
    except Exception:
        return None, []


def _fix_gitflow(repo_url: str) -> list[str]:
    """Create any missing GitFlow branches in the remote repo. Returns list of created branches."""
    import re as _re
    if not repo_url or not cfg.github_token:
        raise RuntimeError("Cannot fix GitFlow: no repo URL or token")

    url_match = _re.search(r"https?://([^/]+)/([^/]+)/([^/]+?)(?:\.git)?$", repo_url)
    if not url_match:
        raise RuntimeError(f"Cannot parse repo URL: {repo_url}")

    host_part, owner, repo = url_match.group(1), url_match.group(2), url_match.group(3)
    cfg_host = _re.sub(r"^https?://", "", cfg.github_url.rstrip("/"))
    if host_part == cfg_host:
        api_base = cfg.github_api_base
    elif "github.com" == host_part:
        api_base = "https://api.github.com"
    else:
        host_url = f"http{'s' if repo_url.startswith('https') else ''}://{host_part}"
        api_base = f"{host_url}/api/v3"

    headers = {
        "Authorization": f"token {cfg.github_token}",
        "Accept": "application/vnd.github.v3+json",
    }

    # Get existing branches and the SHA of the default branch (main)
    branches_resp = _requests.get(f"{api_base}/repos/{owner}/{repo}/branches", headers=headers, timeout=8)
    if branches_resp.status_code != 200:
        raise RuntimeError(f"Could not list branches: HTTP {branches_resp.status_code}")
    existing = {b["name"]: b["commit"]["sha"] for b in branches_resp.json()}
    missing = [b for b in _GITFLOW_BRANCHES if b not in existing]

    created = []
    # Use `main` SHA as base; fall back to first available branch
    base_sha = existing.get("main") or next(iter(existing.values()), None)
    if not base_sha:
        raise RuntimeError("Repository has no commits — cannot create branches")

    for branch in missing:
        resp = _requests.post(
            f"{api_base}/repos/{owner}/{repo}/branches",
            headers=headers,
            json={"new_branch_name": branch, "old_branch_name": "main"},
            timeout=8,
        )
        if resp.status_code not in (200, 201):
            # Gitea uses new_branch_name; GitHub uses ref+sha
            resp = _requests.post(
                f"{api_base}/repos/{owner}/{repo}/git/refs",
                headers=headers,
                json={"ref": f"refs/heads/{branch}", "sha": base_sha},
                timeout=8,
            )
        if resp.status_code in (200, 201):
            created.append(branch)
        else:
            raise RuntimeError(f"Failed to create branch '{branch}': HTTP {resp.status_code} {resp.text[:200]}")

    return created


def _check_jenkins_job(name: str) -> tuple[bool | None, str]:
    """Check whether a multibranch pipeline job exists in Jenkins for this service."""
    if not cfg.jenkins_url or not cfg.jenkins_token:
        return None, "Jenkins not configured (JENKINS_TOKEN not set)"
    try:
        resp = _requests.get(
            f"{cfg.jenkins_url}/job/{name}/api/json",
            auth=(cfg.jenkins_user, cfg.jenkins_token),
            timeout=8,
        )
        if resp.status_code == 200:
            return True, ""
        if resp.status_code == 404:
            return False, f"No Jenkins pipeline found for '{name}'"
        if resp.status_code in (401, 403):
            return None, f"Jenkins access denied (HTTP {resp.status_code}) — check JENKINS_TOKEN"
        return None, f"Jenkins returned HTTP {resp.status_code}"
    except Exception as exc:
        return None, f"Jenkins check failed: {exc}"


# ── Environments ──────────────────────────────────────────────────────────────

@app.get(
    "/api/identity",
    response_model=IdentityResponse,
    tags=["Identity"],
    summary="Resolve acting identity from configured tokens",
)
def get_identity():
    """
    Fetch the identity that will be used for all platform operations.
    Calls GitHub /user and Jenkins /me/api/json to resolve real names.
    Returns warnings when tokens are missing or invalid.
    """
    identity = resolve_identity(cfg)
    return IdentityResponse(**identity.as_dict())


# ── Platform configuration ─────────────────────────────────────────────────────

@app.get("/api/platform/config", response_model=PlatformConfigSchema,
         tags=["Platform"], summary="Get platform integration settings")
def get_platform_config():
    """
    Return current platform-level settings (github_url, github_org, etc.).
    Token values are never returned — only whether each token is set.
    """
    return PlatformConfigSchema(
        github_url=cfg.github_url,
        github_account_type=cfg.github_account_type,
        github_org=cfg.github_org,
        jenkins_url=cfg.jenkins_url,
        github_token_set=bool(cfg.github_token),
        jenkins_token_set=bool(cfg.jenkins_token),
        jenkins_user_set=bool(cfg.jenkins_user),
    )


@app.patch("/api/platform/config", response_model=PlatformConfigSchema,
           tags=["Platform"], summary="Update platform integration settings")
def update_platform_config(body: UpdatePlatformConfigRequest):
    """
    Update non-secret platform settings in platform.yaml.
    Only fields present in the request body are updated.
    Tokens stay in environment variables — not editable via this endpoint.
    """
    import yaml as _yaml
    from pathlib import Path as _Path

    platform_file = _Path(cfg.root) / "platform.yaml"
    with open(platform_file) as f:
        data = _yaml.safe_load(f) or {}

    updates = {
        "github_url":          body.github_url,
        "github_account_type": body.github_account_type,
        "github_org":          body.github_org,
        "jenkins_url":         body.jenkins_url,
    }
    for key, value in updates.items():
        if value is not None:
            data[key] = value
            setattr(cfg, key, value)

    with open(platform_file, "w") as f:
        _yaml.dump(data, f, default_flow_style=False, allow_unicode=True,
                   sort_keys=False)

    return PlatformConfigSchema(
        github_url=cfg.github_url,
        github_account_type=cfg.github_account_type,
        github_org=cfg.github_org,
        jenkins_url=cfg.jenkins_url,
        github_token_set=bool(cfg.github_token),
        jenkins_token_set=bool(cfg.jenkins_token),
        jenkins_user_set=bool(cfg.jenkins_user),
    )


# ── Clusters ──────────────────────────────────────────────────────────────────

def _profile_to_schema(name: str) -> ClusterSchema:
    p = cfg.get_cluster_profile(name)
    return ClusterSchema(
        name=p.name, platform=p.platform,
        registry=p.registry, helm_values_suffix=p.helm_values_suffix,
        in_use=cfg.cluster_in_use(p.name),
        api_url=p.api_url or None, context=p.context or None,
        region=p.region or None, cluster_name=p.cluster_name or None,
    )


@app.get("/api/clusters", response_model=list[ClusterSchema],
         tags=["Clusters"], summary="List all cluster profiles")
def list_clusters():
    """Return all cluster profiles declared in platform.yaml, with in-use environment list."""
    return [_profile_to_schema(name) for name in sorted(cfg.clusters)]


@app.get("/api/clusters/{name}", response_model=ClusterSchema,
         tags=["Clusters"], summary="Get a cluster profile")
def get_cluster(name: str):
    if name not in cfg.clusters:
        raise HTTPException(status_code=404, detail=f"Cluster '{name}' not found")
    return _profile_to_schema(name)


@app.post("/api/clusters", response_model=ClusterSchema, status_code=201,
          tags=["Clusters"], summary="Add a cluster profile")
def add_cluster(body: AddClusterRequest):
    """Add a new cluster profile to platform.yaml."""
    if body.name in cfg.clusters:
        raise HTTPException(status_code=409,
                            detail=f"Cluster '{body.name}' already exists")
    from config import ClusterProfile
    registry = body.registry or cfg.registries.get(body.platform, cfg.registry)
    suffix   = body.helm_values_suffix or body.name.split("-")[-1]
    profile  = ClusterProfile(
        name=body.name, platform=body.platform,
        registry=registry, helm_values_suffix=suffix,
        api_url=body.api_url, context=body.context,
        region=body.region, cluster_name=body.cluster_name,
    )
    cfg.save_cluster(profile)
    return _profile_to_schema(body.name)


@app.put("/api/clusters/{name}", response_model=ClusterSchema,
         tags=["Clusters"], summary="Update a cluster profile")
def update_cluster(name: str, body: AddClusterRequest):
    """Update an existing cluster profile. All fields are replaced."""
    if name not in cfg.clusters:
        raise HTTPException(status_code=404, detail=f"Cluster '{name}' not found")
    from config import ClusterProfile
    registry = body.registry or cfg.registries.get(body.platform, cfg.registry)
    suffix   = body.helm_values_suffix or body.name.split("-")[-1]
    profile  = ClusterProfile(
        name=name, platform=body.platform,
        registry=registry, helm_values_suffix=suffix,
        api_url=body.api_url, context=body.context,
        region=body.region, cluster_name=body.cluster_name,
    )
    cfg.save_cluster(profile)
    return _profile_to_schema(name)


@app.delete("/api/clusters/{name}", tags=["Clusters"],
            summary="Remove a cluster profile")
def delete_cluster(name: str, force: bool = False):
    """Remove a cluster profile. Fails if environments still reference it (unless force=true)."""
    if name not in cfg.clusters:
        raise HTTPException(status_code=404, detail=f"Cluster '{name}' not found")
    in_use = cfg.cluster_in_use(name)
    if in_use and not force:
        raise HTTPException(status_code=409,
                            detail=f"Cluster '{name}' is used by: {', '.join(in_use)}. "
                                   "Use ?force=true to remove anyway.")
    cfg.delete_cluster(name)
    return {"status": "deleted", "name": name}


@app.get(
    "/api/history",
    response_model=list[AuditEventSchema],
    tags=["History"],
    summary="Platform audit log — all actions across envs and services",
)
def get_history(
    env:     str | None = None,
    service: str | None = None,
    actor:   str | None = None,
    type:    str | None = None,
    limit:   int = 100,
    full:    bool = False,
):
    """
    Return platform audit events, newest first.

    Sources:
    - `git log envs/` — environment lifecycle events (create, destroy, update)
    - `versions.yaml` `deployed_at` fields — deployment events per service

    All query parameters are optional and can be combined.
    """
    collector = HistoryCollector(cfg)
    events = collector.collect(
        env_filter=env,
        service_filter=service,
        actor_filter=actor,
        event_type_filter=type,
        limit=min(limit, 500),
        full=full,
    )
    return [
        AuditEventSchema(label=e.label, **e.as_dict())
        for e in events
    ]


@app.get("/api/envs", response_model=list[EnvSummary], tags=["Environments"],
         summary="List all environments")
def list_envs():
    """Return all environments (fixed + POC) with their deployed service versions."""
    return [_build_env_summary(e) for e in cfg.list_envs()]


@app.get("/api/envs/{name}", response_model=EnvSummary, tags=["Environments"],
         summary="Get environment details")
def get_env(name: str):
    """Return full details for a single environment."""
    if not cfg.env_path(name).exists():
        raise HTTPException(status_code=404, detail=f"Environment '{name}' not found")
    return _build_env_summary(name)


@app.post("/api/envs", response_model=CreateEnvResponse, status_code=201,
          tags=["Environments"], summary="Create a POC environment")
def create_env(body: CreateEnvRequest):
    """
    Fork an existing environment into a new ephemeral POC namespace.
    The full name is auto-generated as `poc-{name}-{YYYYMMDD}`.

    **Non-fatal warnings** (missing tokens, git commit failure, etc.) are returned
    in the `warnings` array. The environment YAML is always written when 201 is
    returned — warnings require manual follow-up but do not prevent creation.
    """
    mgr = EnvManager(cfg)
    full_name = mgr._poc_name(body.name)
    if cfg.env_path(full_name).exists():
        raise HTTPException(status_code=409, detail=f"Environment '{full_name}' already exists")
    try:
        result = mgr.create(
            name=body.name, env_type="poc", base=body.base,
            namespace=body.namespace,
            cluster=body.cluster,
            platform=body.platform,
            owner=body.owner, description=body.description,
            ttl_days=body.ttl_days,
            force=body.force,
        )
    except SystemExit:
        raise HTTPException(status_code=400, detail="Creation failed — check that the base env exists")
    return CreateEnvResponse(
        status="created",
        name=result.get("name", full_name),
        warnings=result.get("warnings", []),
    )


@app.delete("/api/envs/{name}", response_model=DestroyResponse, tags=["Environments"],
            summary="Destroy a POC environment")
def destroy_env(name: str):
    """
    Destroy a POC environment: deletes the OpenShift namespace and removes
    the entry from platform-config. Only POC environments can be destroyed.
    """
    try:
        data = cfg.load_versions(name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Environment '{name}' not found")
    if data.get("_meta", {}).get("env_type", "fixed") == "fixed":
        raise HTTPException(status_code=403, detail="Cannot destroy fixed environments")
    EnvManager(cfg).destroy(name, force=True)
    return DestroyResponse(status="destroyed", name=name)


@app.post("/api/envs/{name}/extend", response_model=ExtendEnvResponse,
          tags=["Environments"], summary="Postpone TTL expiry of a POC environment")
def extend_env(name: str, body: ExtendEnvRequest):
    """
    Add `ttl_days` to the current `expires_at` of a POC environment.
    The new expiry is capped at 365 days from today.

    Expiry is a **soft deadline** — environments are never destroyed automatically.
    This endpoint lets you postpone the deadline when the warning appears.
    """
    try:
        data = cfg.load_versions(name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Environment '{name}' not found")
    if data.get("_meta", {}).get("env_type", "fixed") != "poc":
        raise HTTPException(status_code=400, detail="Only POC environments have a TTL")

    EnvManager(cfg).extend(name, ttl_days=body.ttl_days)

    # Re-read to get the updated expiry
    updated = cfg.load_versions(name)
    from env_manager import EnvManager as _EM
    expiry = _EM(cfg)._expiry_status(updated)
    new_expires = updated["_meta"].get("expires_at", "")
    return ExtendEnvResponse(
        status="extended",
        name=name,
        expires_at=new_expires,
        days_remaining=expiry.get("days_remaining", 0),
    )


@app.get("/api/envs/{env_from}/diff/{env_to}", tags=["Environments"],
         summary="Diff service versions between two environments")
def env_diff(env_from: str, env_to: str) -> list[dict]:
    """
    Compare service versions between two environments.
    Each entry contains the version in each env and a boolean `changed` flag.
    """
    try:
        from_data = cfg.load_versions(env_from)
        to_data = cfg.load_versions(env_to)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    from_svcs = from_data.get("services", {})
    to_svcs = to_data.get("services", {})
    return [
        {
            "service": svc,
            env_from: from_svcs.get(svc, {}).get("version", "--"),
            env_to: to_svcs.get(svc, {}).get("version", "--"),
            "changed": from_svcs.get(svc, {}).get("version") != to_svcs.get(svc, {}).get("version"),
        }
        for svc in sorted(set(list(from_svcs) + list(to_svcs)))
    ]


# ── Services ──────────────────────────────────────────────────────────────────

@app.get("/api/services", response_model=list[ServiceSummary], tags=["Services"],
         summary="List all services across all environments")
def list_services():
    """Aggregate service versions across every environment."""
    return _collect_all_services()


@app.get("/api/services/hosted", response_model=list[str], tags=["Services"],
         summary="List AP3-hosted services available for forking")
def list_hosted_services():
    """
    Return the names of services that are AP3-hosted (i.e. their repo lives in
    the AP3 GitHub org). These can be used as fork sources when creating a new
    service in 'fork' mode. Determined by checking .ap3/hooks.yaml in versions.yaml.
    Falls back to listing all known services if hooks metadata is unavailable.
    """
    services = _collect_all_services()
    return sorted([s.name for s in services])


@app.get("/api/services/{name}", response_model=ServiceSummary, tags=["Services"],
         summary="Get service details including repository reachability check")
def get_service(name: str):
    """
    Return version matrix for a single service across all environments.
    Also checks whether the registered repository URL is reachable.
    `repo_exists` is `true` (accessible), `false` (404 / gone), or `null` (no URL / unreachable).
    `repo_warning` is non-empty when the check found a problem.
    """
    svc = next((s for s in _collect_all_services() if s.name == name), None)
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service '{name}' not found")
    exists, warning = _check_repo_exists(svc.repo_url)
    svc.repo_exists = exists
    svc.repo_warning = warning
    if exists:
        gf_ok, gf_missing = _check_gitflow(svc.repo_url)
        svc.gitflow_ok = gf_ok
        svc.gitflow_missing = gf_missing
    jk_ok, jk_warning = _check_jenkins_job(name)
    svc.jenkins_ok = jk_ok
    svc.jenkins_warning = jk_warning
    return svc


@app.post("/api/services/{name}/fix-gitflow", tags=["Services"],
          summary="Create any missing GitFlow branches in the service repository")
def fix_service_gitflow(name: str):
    """
    Create `main` and/or `develop` branches if they are absent from the service repo.
    Returns the list of branches that were created.
    Raises 404 if the service is not registered, 400 if the repo is unreachable.
    """
    svc = next((s for s in _collect_all_services() if s.name == name), None)
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service '{name}' not found")
    if not svc.repo_url:
        raise HTTPException(status_code=400, detail="Service has no registered repository URL")
    try:
        created = _fix_gitflow(svc.repo_url)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"created": created}


@app.post("/api/services/{name}/fix-jenkins", tags=["Services"],
          summary="Create a Jenkins multibranch pipeline for this service")
def fix_service_jenkins(name: str):
    """
    Create (or re-create) the Jenkins multibranch pipeline for this service.
    The job is pointed at the repo URL stored in the service catalog.
    Returns 404 if the service is not registered, 400 if Jenkins is unreachable or creation fails.
    """
    svc = next((s for s in _collect_all_services() if s.name == name), None)
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service '{name}' not found")
    if not cfg.jenkins_url or not cfg.jenkins_token:
        raise HTTPException(status_code=400, detail="Jenkins not configured — set JENKINS_URL and JENKINS_TOKEN")
    try:
        import sys as _sys
        _sys.path.insert(0, str(cfg.root / "scripts"))
        from service_creator import ServiceCreator
        creator = ServiceCreator(cfg)
        creator._register_jenkins_pipeline(name, svc.repo_url)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"created": name}


@app.get(
    "/api/services/{name}/history",
    response_model=ServiceHistoryResponse,
    tags=["Services"],
    summary="Get commit log, releases and deployment history for a service",
)
def get_service_history(name: str, limit: int = 30):
    """
    Returns three sources merged:
    - GitHub commits (newest first, up to `limit`) with tag annotations
    - GitHub releases / tags
    - Platform deployment events from the audit log

    `github_available` is false when GITHUB_TOKEN is not set or the repo is
    unreachable; deployment history is always returned regardless.
    """
    import re as _re

    # ── Platform deployment history (always available) ─────────────────────
    from history import HistoryCollector
    events = HistoryCollector(cfg).collect(service_filter=name, limit=200)
    deployments = [
        ServiceDeployEvent(
            env=e.env,
            version=e.version or "—",
            deployed_at=e.timestamp,
            actor=e.actor or "unknown",
        )
        for e in events
        if e.event_type == "deploy" and e.version
    ]

    # ── Resolve repo owner/name ────────────────────────────────────────────
    repo_url = ""
    github_owner = cfg.github_account
    github_repo  = name
    api_base     = cfg.github_api_base

    try:
        catalog = cfg.load_service(name)
        repo_url = catalog.get("repo_url", "")
        if repo_url:
            # Parse owner/repo from URL
            m = _re.match(
                r"https?://([^/]+)/([^/]+)/([^/]+?)(?:\.git)?$", repo_url
            )
            if m:
                host_part    = m.group(1)
                github_owner = m.group(2)
                github_repo  = m.group(3)
                # Derive API base — use cfg.github_api_base when host matches config
                cfg_host = _re.sub(r"^https?://", "", cfg.github_url.rstrip("/"))
                if host_part == cfg_host:
                    api_base = cfg.github_api_base
                elif "github.com" == host_part:
                    api_base = "https://api.github.com"
                else:
                    host_url = _re.match(r"(https?://[^/]+)", repo_url).group(1)
                    api_base = f"{host_url}/api/v3"
    except FileNotFoundError:
        pass  # service not in catalog — use defaults

    if not repo_url:
        repo_url = f"{cfg.github_url.rstrip('/')}/{github_owner}/{github_repo}"

    # ── GitHub data ────────────────────────────────────────────────────────
    if not cfg.github_token:
        return ServiceHistoryResponse(
            deployments=deployments,
            github_available=False,
            repo_url=repo_url,
        )

    headers = {
        "Authorization": f"token {cfg.github_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    repo_api = f"{api_base}/repos/{github_owner}/{github_repo}"

    # Fetch tags (to annotate commits)
    tag_by_sha: dict[str, list[str]] = {}
    try:
        tag_resp = _requests.get(f"{repo_api}/tags", headers=headers,
                                 params={"per_page": 100}, timeout=8)
        if tag_resp.status_code == 200:
            for t in tag_resp.json():
                sha = t.get("commit", {}).get("sha", "")
                tag_by_sha.setdefault(sha, []).append(t["name"])
    except Exception:
        pass

    # Fetch releases
    releases: list[ReleaseInfo] = []
    try:
        rel_resp = _requests.get(f"{repo_api}/releases", headers=headers,
                                 params={"per_page": 50}, timeout=8)
        if rel_resp.status_code == 200:
            for r in rel_resp.json():
                releases.append(ReleaseInfo(
                    tag=r.get("tag_name", ""),
                    name=r.get("name") or r.get("tag_name", ""),
                    date=r.get("published_at") or r.get("created_at", ""),
                    notes=r.get("body") or "",
                ))
    except Exception:
        pass

    # If no releases, fall back to lightweight tags
    if not releases and tag_by_sha:
        try:
            tags_resp = _requests.get(f"{repo_api}/tags", headers=headers,
                                      params={"per_page": 50}, timeout=8)
            if tags_resp.status_code == 200:
                for t in tags_resp.json():
                    releases.append(ReleaseInfo(
                        tag=t["name"], name=t["name"],
                        date="",  # lightweight tags carry no date
                    ))
        except Exception:
            pass

    # Fetch commits
    commits: list[CommitInfo] = []
    try:
        c_resp = _requests.get(f"{repo_api}/commits", headers=headers,
                               params={"per_page": limit}, timeout=10)
        if c_resp.status_code == 200:
            for c in c_resp.json():
                sha  = c.get("sha", "")
                info = c.get("commit", {})
                author_info = info.get("author") or {}
                gh_author   = (c.get("author") or {}).get("login", "")
                author = gh_author or author_info.get("email", "unknown")
                full_msg = info.get("message", "")
                commits.append(CommitInfo(
                    sha=sha,
                    short_sha=sha[:7],
                    message=full_msg.split("\n")[0],
                    author=author,
                    date=author_info.get("date", ""),
                    tags=tag_by_sha.get(sha, []),
                ))
        elif c_resp.status_code == 404:
            return ServiceHistoryResponse(
                deployments=deployments,
                releases=releases,
                github_available=False,
                repo_url=repo_url,
            )
    except Exception:
        pass

    return ServiceHistoryResponse(
        commits=commits,
        releases=releases,
        deployments=deployments,
        github_available=True,
        repo_url=repo_url,
    )


@app.post("/api/services", response_model=CreateServiceResponse, status_code=201,
          tags=["Services"], summary="Bootstrap a new AP3 service")
def create_service(body: CreateServiceRequest):
    """
    Delegates to `platform_cli.py service create` — the CLI is the single authority
    for service bootstrapping (scaffolding, GitHub repo creation, Jenkins registration).
    Non-fatal issues (missing tokens, skipped steps) are returned in `warnings`.
    """
    args = [
        "service", "create",
        "--name",  body.name,
        "--owner", body.owner,
        "--force",
    ]
    if body.description:
        args += ["--description", body.description]
    if body.source_mode == "fork":
        args += ["--fork-from", body.fork_from]
    elif body.source_mode == "external":
        args += ["--external-repo", body.external_repo_url]
    else:
        args += ["--template", body.template]
    # External repos are not AP3-managed — always skip Jenkins job creation
    if body.skip_jenkins or body.source_mode == "external":
        args.append("--no-jenkins")

    result = _run_cli(*args)
    return CreateServiceResponse(
        status="created",
        name=body.name,
        warnings=result.get("warnings", []),
        steps=result.get("steps", []),
    )


@app.delete(
    "/api/services/{name}",
    response_model=RemoveServiceResponse,
    tags=["Services"],
    summary="Remove a service from all environments",
)
def remove_service(name: str):
    """
    Remove a service from every environment, destroy its Jenkins pipeline,
    and remove any AP3/Jenkins webhooks from the GitHub repo.
    The GitHub repository itself is **not** deleted.

    Non-fatal issues (missing tokens, skipped steps) are returned in `warnings`.
    """
    svc = next((s for s in _collect_all_services() if s.name == name), None)
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service '{name}' not found")
    result = _run_cli("service", "remove", "--name", name, "--force")
    return RemoveServiceResponse(
        status="removed",
        name=name,
        envs=result.get("envs", []),
        warnings=result.get("warnings", []),
        steps=result.get("steps", []),
    )


@app.get(
    "/api/github/repos",
    response_model=list[GitHubRepoInfo],
    tags=["Services"],
    summary="List GitHub repos not yet registered in the platform",
)
def list_unregistered_github_repos():
    """
    Return GitHub repositories for the configured org/user that are not already
    registered as platform services (neither in the service catalog nor in any
    environment's versions.yaml). Useful for recovering existing projects.
    Requires GITHUB_TOKEN to be set.
    """
    if not cfg.github_token:
        raise HTTPException(
            status_code=503,
            detail="GITHUB_TOKEN is not set — cannot query GitHub API",
        )

    # Build the set of already-known service names
    known: set[str] = set(cfg.list_service_names())
    for env in cfg.list_envs():
        try:
            data = cfg.load_versions(env)
            known.update((data.get("services") or {}).keys())
        except Exception:
            pass

    # Fetch all repos for the account (paginated, up to 1000)
    headers = {
        "Authorization": f"token {cfg.github_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    if cfg.github_account_type == "org":
        base_url = f"{cfg.github_api_base}/orgs/{cfg.github_account}/repos"
    else:
        base_url = f"{cfg.github_api_base}/user/repos"

    repos: list[dict] = []
    page = 1
    while True:
        resp = _requests.get(
            base_url,
            headers=headers,
            params={"type": "all", "per_page": 100, "page": page},
            timeout=10,
        )
        if resp.status_code == 401:
            raise HTTPException(status_code=503, detail="GitHub token is invalid or expired")
        if resp.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"GitHub API error {resp.status_code}: {resp.text[:200]}",
            )
        batch = resp.json()
        if not batch:
            break
        repos.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    return [
        GitHubRepoInfo(
            name=r["name"],
            clone_url=r["clone_url"],
            description=r.get("description") or "",
            language=r.get("language") or "",
            updated_at=r.get("updated_at"),
            private=r.get("private", False),
        )
        for r in repos
        if r["name"] not in known
    ]


# ── Status ────────────────────────────────────────────────────────────────────

@app.get(
    "/api/status",
    response_model=list[EnvLiveStatusSchema],
    tags=["Status"],
    summary="Live status of all environments vs expected state",
)
def get_all_status():
    """
    For every environment, compare the expected service versions in versions.yaml
    against the actual running deployments on the cluster.

    Token resolution per cluster (in order):
      1. `{CLUSTER_NAME_UPPER}_TOKEN`  — e.g. `OPENSHIFT_PROD_TOKEN`
      2. `OC_TOKEN`                    — OpenShift global fallback
      3. `KUBE_TOKEN`                  — generic Kubernetes fallback

    When no token and no kubeconfig context is available, the environment is
    reported as `unknown` (unreachable) without raising an error.
    """
    from status_checker import StatusChecker
    results = StatusChecker(cfg).check_all()
    return [EnvLiveStatusSchema(**r.as_dict()) for r in results]


@app.get(
    "/api/status/{env_name}",
    response_model=EnvLiveStatusSchema,
    tags=["Status"],
    summary="Live status of a single environment",
)
def get_env_status(env_name: str):
    """Live status check for one environment. Same token resolution as GET /api/status."""
    if not cfg.env_path(env_name).exists():
        raise HTTPException(status_code=404, detail=f"Environment '{env_name}' not found")
    from status_checker import StatusChecker
    result = StatusChecker(cfg).check_env(env_name)
    return EnvLiveStatusSchema(**result.as_dict())


@app.get(
    "/api/envs/{env_name}/pods",
    response_model=EnvPodsSchema,
    tags=["Status"],
    summary="Live pod list for an environment namespace",
)
def get_env_pods(env_name: str):
    """
    Fetch all pods currently running in the environment's Kubernetes namespace.
    Uses the same token resolution as the status endpoints.
    """
    if not cfg.env_path(env_name).exists():
        raise HTTPException(status_code=404, detail=f"Environment '{env_name}' not found")
    from status_checker import StatusChecker
    result = StatusChecker(cfg).check_env_pods(env_name)
    return EnvPodsSchema(**result.as_dict())


@app.get(
    "/api/clusters/{cluster_name}/live",
    response_model=ClusterLiveSchema,
    tags=["Clusters"],
    summary="Live cluster view: nodes and pods grouped by namespace",
)
def get_cluster_live(cluster_name: str):
    """
    Fetch live cluster state: node status + all pods across all namespaces
    grouped by namespace. Useful for a full cluster topology view.
    """
    known = {c.name for c in cfg.list_clusters()}
    if cluster_name not in known:
        raise HTTPException(status_code=404, detail=f"Cluster '{cluster_name}' not found")
    from status_checker import StatusChecker
    result = StatusChecker(cfg).check_cluster_live(cluster_name)
    return ClusterLiveSchema(**result.as_dict())


# ── Deployments ───────────────────────────────────────────────────────────────

@app.post("/api/deploy", response_model=DeployResponse, tags=["Deployments"],
          summary="Trigger a service deployment")
def deploy(body: DeployRequest):
    """
    Deploy a specific version of a service to a target environment.

    - If JENKINS_TOKEN is set: triggers a Jenkins parameterised build.
    - Otherwise: runs helm upgrade --install directly (useful for POC envs).

    Updates versions.yaml in platform-config after a successful trigger.
    """
    args = [
        "deploy",
        "--env",     body.env,
        "--service", body.service,
        "--version", body.version,
        "--force",
    ]
    if body.wait:
        args.append("--wait")
    if body.platform:
        args += ["--platform", body.platform]
    _run_cli(*args)
    return DeployResponse(status="triggered", env=body.env,
                          service=body.service, version=body.version)


# ── Deployment requests (GitOps pull model) ───────────────────────────────────

@app.post("/api/envs/{env_name}/deploy-requests",
          response_model=DeployRequestV2Response,
          tags=["Deployments"],
          summary="Request a service deployment for an environment")
def create_deploy_request(env_name: str, body: DeployRequestV2):
    """
    Declare a desired deployment in versions.yaml (GitOps pull model).

    - Stores the request under `requested_deployments` in envs/{env}/versions.yaml.
    - When version is 'latest', Jenkins will auto-execute on the next successful build.
    - For specific versions, use POST /api/envs/{env}/deploy-requests/{service}/execute
      or wait for an operator to execute it via the CLI.
    """
    if env_name not in cfg.list_envs():
        raise HTTPException(status_code=404, detail=f"Environment '{env_name}' not found")
    _run_cli(
        "deploy", "request",
        "--env",     env_name,
        "--service", body.service,
        "--version", body.version,
        "--force",
    )
    # Re-read to get the recorded timestamp
    data = cfg.load_versions(env_name)
    req = data.get("requested_deployments", {}).get(body.service, {})
    return DeployRequestV2Response(
        status="requested",
        env=env_name,
        service=body.service,
        requested_version=req.get("requested_version", body.version),
        auto=req.get("auto", body.version == "latest"),
        requested_at=req.get("requested_at", ""),
    )


@app.get("/api/envs/{env_name}/deploy-requests",
         response_model=list[DeployRequestStatus],
         tags=["Deployments"],
         summary="List deployment requests for an environment")
def list_deploy_requests(env_name: str):
    """Return all deployment requests (pending, fulfilled, cancelled) for this environment."""
    if env_name not in cfg.list_envs():
        raise HTTPException(status_code=404, detail=f"Environment '{env_name}' not found")
    data = cfg.load_versions(env_name)
    requests = data.get("requested_deployments") or {}
    return [
        DeployRequestStatus(
            service=svc,
            requested_version=r.get("requested_version", ""),
            requested_at=r.get("requested_at", ""),
            requested_by=r.get("requested_by", ""),
            auto=r.get("auto", False),
            status=r.get("status", "pending"),
            fulfilled_version=r.get("fulfilled_version"),
            fulfilled_at=r.get("fulfilled_at"),
        )
        for svc, r in requests.items()
    ]


@app.delete("/api/envs/{env_name}/deploy-requests/{service}",
            tags=["Deployments"],
            summary="Cancel a pending deployment request")
def cancel_deploy_request(env_name: str, service: str):
    """Remove a pending deployment request from versions.yaml."""
    if env_name not in cfg.list_envs():
        raise HTTPException(status_code=404, detail=f"Environment '{env_name}' not found")
    _run_cli(
        "deploy", "cancel",
        "--env",     env_name,
        "--service", service,
        "--force",
    )
    return {"status": "cancelled", "env": env_name, "service": service}


# ── Templates ─────────────────────────────────────────────────────────────────

def _template_info(name: str, tdir: Path) -> TemplateInfo:
    """Build a TemplateInfo from a template directory, preferring template.yaml."""
    import yaml as _yaml
    meta_file = tdir / "template.yaml"
    if meta_file.exists():
        meta = _yaml.safe_load(meta_file.read_text()) or {}
    else:
        meta = {}
        readme = tdir / "README.md"
        if readme.exists():
            meta["description"] = readme.read_text(errors="replace").split("\n")[0].lstrip("# ").strip()
    return TemplateInfo(
        id=name,
        description=meta.get("description", ""),
        language=meta.get("language", ""),
        created_at=meta.get("created_at"),
        created_by=meta.get("created_by"),
    )


@app.get("/api/templates", response_model=list[TemplateInfo], tags=["Templates"],
         summary="List available scaffold templates")
def list_templates():
    """Return all scaffold templates with metadata from template.yaml (falls back to README.md)."""
    if not cfg.templates_dir.exists():
        return []
    return [
        _template_info(t.name, t)
        for t in sorted(cfg.templates_dir.iterdir())
        if t.is_dir()
    ]


@app.get("/api/templates/{name}", response_model=TemplateInfo, tags=["Templates"],
         summary="Get a scaffold template")
def get_template(name: str):
    tdir = cfg.templates_dir / name
    if not tdir.is_dir():
        raise HTTPException(status_code=404, detail=f"Template '{name}' not found")
    return _template_info(name, tdir)


@app.post("/api/templates", response_model=TemplateInfo, status_code=201,
          tags=["Templates"], summary="Add a scaffold template from a server-side directory")
def add_template(body: AddTemplateRequest):
    """
    Copies a directory into templates/<name>/ and commits the addition.
    Delegates to `platform_cli.py template add` so the git commit records the actor.
    `from_dir` must be a path accessible on the server running the API.
    """
    args = [
        "template", "add",
        "--name",    body.name,
        "--from-dir", body.from_dir,
        "--force",
    ]
    if body.description:
        args += ["--description", body.description]
    if body.language:
        args += ["--language", body.language]
    _run_cli(*args)
    tdir = cfg.templates_dir / body.name
    if not tdir.is_dir():
        raise HTTPException(status_code=500, detail="Template directory not found after add")
    return _template_info(body.name, tdir)


@app.delete("/api/templates/{name}", tags=["Templates"],
            summary="Remove a scaffold template")
def remove_template(name: str, force: bool = False):
    """
    Deletes templates/<name>/ and commits the removal.
    Delegates to `platform_cli.py template remove` so the git commit records the actor.
    Fails if any service in the catalog still references this template (unless force=true).
    """
    tdir = cfg.templates_dir / name
    if not tdir.is_dir():
        raise HTTPException(status_code=404, detail=f"Template '{name}' not found")
    args = ["template", "remove", "--name", name, "--force"] if force else \
           ["template", "remove", "--name", name]
    _run_cli(*args)
    return {"status": "removed", "name": name}


# ── Static / SPA ─────────────────────────────────────────────────────────────

if FRONTEND_DIST.exists():
    assets = FRONTEND_DIST / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str):
        index = FRONTEND_DIST / "index.html"
        if index.exists():
            return FileResponse(str(index))
        raise HTTPException(status_code=404, detail="Frontend not built -- run: npm run build")
else:
    @app.get("/", include_in_schema=False)
    def root():
        return {
            "status": "Platform API running -- frontend not built yet",
            "tip": "Run `npm run build` in dashboard/frontend/ to serve the UI here",
            "swagger_ui": "/docs",
            "redoc": "/redoc",
        }


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 5173))
    reload = os.environ.get("RELOAD", "1") == "1"

    print(f"\n  Platform Dashboard API")
    print(f"  -> http://localhost:{port}")
    print(f"  -> http://localhost:{port}/docs   (Swagger UI)")
    print(f"  -> http://localhost:{port}/redoc  (ReDoc)\n")

    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=reload,
                reload_dirs=[str(SCRIPTS_DIR)])
