"""config.py — Platform configuration loader."""

import os
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


# Valid platform identifiers
PLATFORMS = ("openshift", "aws")


def _find_platform_root() -> Path:
    """Walk up from cwd to find the platform repo root (contains envs/ dir)."""
    p = Path.cwd()
    for _ in range(8):
        if (p / "envs").is_dir() and (p / "scripts").is_dir():
            return p
        p = p.parent
    return Path(__file__).parent.parent


@dataclass
class ClusterProfile:
    """Resolved metadata for a single cluster."""
    name: str
    platform: str                    # "openshift" | "aws"
    registry: str
    helm_values_suffix: str          # e.g. "dev" → helm/values-dev.yaml

    # OpenShift-specific
    api_url: str = ""
    context: str = ""

    # AWS-specific
    region: str = ""
    cluster_name: str = ""

    @property
    def is_openshift(self) -> bool:
        return self.platform == "openshift"

    @property
    def is_aws(self) -> bool:
        return self.platform == "aws"


@dataclass
class PlatformConfig:
    config_path: Optional[str] = None

    # Resolved at init
    root: Path = field(init=False)
    envs_dir: Path = field(init=False)
    templates_dir: Path = field(init=False)
    scripts_dir: Path = field(init=False)

    # From platform.yaml
    shared_lib_version: str = "main"
    github_url: str = "https://github.com"
    github_account_type: str = "org"   # "org" | "user"
    github_org: str = "my-org"         # org name or username
    github_token_env: str = "GITHUB_TOKEN"
    github_api_path: str = ""          # override API path segment (e.g. "api/v1" for Gitea)
    jenkins_url: str = "https://jenkins.internal"
    jenkins_git_url: str = ""              # URL Jenkins uses internally to clone repos
                                           # (may differ from github_url when Jenkins is in Docker)
    jenkins_hook_url: str = ""             # URL the git server uses to reach Jenkins for webhooks
                                           # (may differ from jenkins_url when both are in Docker)
    jenkins_user_env: str = "JENKINS_USER"
    jenkins_token_env: str = "JENKINS_TOKEN"
    shared_lib_url: str = ""          # full Jenkins-facing URL for jenkins-shared-lib repo
    sonarqube_url: str = ""           # SonarQube base URL (used by delete flow)

    @property
    def resolved_shared_lib_url(self) -> str:
        """Return the URL Jenkins uses to clone the shared library.

        Uses shared_lib_url if set; otherwise derives it from jenkins_git_url / github_url
        using the conventional repo name 'jenkins-shared-lib'.
        """
        if self.shared_lib_url:
            return self.shared_lib_url
        lib_base = (self.jenkins_git_url or self.github_url).rstrip("/")
        return f"{lib_base}/{self.github_org}/jenkins-shared-lib.git"

    @property
    def github_account(self) -> str:
        """Return the org or username used for repo creation."""
        return self.github_org

    @property
    def github_api_base(self) -> str:
        """Return the Git service REST API base URL.

        Handles three cases:
          - github.com            → https://api.github.com
          - GitHub Enterprise     → https://hostname/api/v3
          - Gitea / other         → https://hostname/<github_api_path>  (e.g. api/v1)
        """
        base = self.github_url.rstrip("/")
        if "github.com" in base:
            return "https://api.github.com"
        if self.github_api_path:
            return f"{base}/{self.github_api_path.lstrip('/')}"
        # Default: GitHub Enterprise Server style
        return f"{base}/api/v3"

    def github_repos_api(self) -> str:
        """Return the endpoint for creating a new repo under the configured account."""
        api = self.github_api_base
        if self.github_account_type == "user":
            return f"{api}/user/repos"
        return f"{api}/orgs/{self.github_org}/repos"
    registry: str = "registry.internal"          # legacy fallback
    default_cluster_dev: str = "openshift-dev"
    default_cluster_staging: str = "openshift-staging"
    default_cluster_prod: str = "openshift-prod"
    default_cluster_poc: str = "openshift-dev"
    clusters: dict = field(default_factory=dict)
    registries: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.config_path:
            cfg_file = Path(self.config_path)
            self.root = cfg_file.parent if cfg_file.exists() else _find_platform_root()
        else:
            cfg_file = None
            self.root = _find_platform_root()
        self.envs_dir = self.root / "envs"
        self.services_dir = self.root / "services"
        self.templates_dir = self.root / "templates"
        self.scripts_dir = self.root / "scripts"

        if cfg_file is None:
            cfg_file = self.root / "platform.yaml"
        if cfg_file.exists():
            with open(cfg_file) as f:
                data = yaml.safe_load(f) or {}
            for k, v in data.items():
                if hasattr(self, k):
                    setattr(self, k, v)

    # ── Tokens ────────────────────────────────────────────────────────────────

    @property
    def github_token(self) -> Optional[str]:
        return os.environ.get(self.github_token_env)

    @property
    def jenkins_token(self) -> Optional[str]:
        return os.environ.get(self.jenkins_token_env)

    @property
    def jenkins_user(self) -> Optional[str]:
        return os.environ.get(self.jenkins_user_env)

    # ── Cluster profiles ──────────────────────────────────────────────────────

    def get_cluster_profile(self, cluster_name: str) -> ClusterProfile:
        """
        Resolve a cluster name to a ClusterProfile.
        Falls back to a minimal openshift profile if the cluster is not declared
        in platform.yaml (e.g. legacy single-cluster setups).
        """
        raw = self.clusters.get(cluster_name)
        if not raw:
            # Best-effort fallback: infer platform from cluster name
            platform = "aws" if "eks" in cluster_name.lower() else "openshift"
            registry = self.registries.get(platform, self.registry)
            return ClusterProfile(
                name=cluster_name,
                platform=platform,
                registry=registry,
                helm_values_suffix=cluster_name.split("-")[-1],  # e.g. "dev"
            )
        platform = raw.get("platform", "openshift")
        return ClusterProfile(
            name=cluster_name,
            platform=platform,
            registry=raw.get("registry", self.registries.get(platform, self.registry)),
            helm_values_suffix=raw.get("helm_values_suffix", "dev"),
            api_url=raw.get("api_url", ""),
            context=raw.get("context", ""),
            region=raw.get("region", ""),
            cluster_name=raw.get("cluster_name", ""),
        )

    def default_cluster_for(self, env_type: str) -> str:
        """Return the default cluster name for an environment type."""
        return {
            "dev":     self.default_cluster_dev,
            "staging": self.default_cluster_staging,
            "prod":    self.default_cluster_prod,
            "poc":     self.default_cluster_poc,
        }.get(env_type, self.default_cluster_dev)

    def registry_for_cluster(self, cluster_name: str) -> str:
        """Return the image registry URL for a given cluster."""
        return self.get_cluster_profile(cluster_name).registry

    # ── Env helpers ───────────────────────────────────────────────────────────

    def env_path(self, env_name: str) -> Path:
        return self.envs_dir / env_name

    def env_versions_path(self, env_name: str) -> Path:
        return self.env_path(env_name) / "versions.yaml"

    def list_envs(self) -> list[str]:
        if not self.envs_dir.exists():
            return []
        return sorted(
            d.name for d in self.envs_dir.iterdir()
            if d.is_dir() and (d / "versions.yaml").exists()
        )

    def load_versions(self, env_name: str) -> dict:
        path = self.env_versions_path(env_name)
        if not path.exists():
            raise FileNotFoundError(f"Environment '{env_name}' not found at {path}")
        with open(path) as f:
            return yaml.safe_load(f) or {}

    def save_versions(self, env_name: str, data: dict):
        path = self.env_versions_path(env_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)

    # ── Service catalog ───────────────────────────────────────────────────────

    def service_catalog_path(self, name: str) -> Path:
        return self.services_dir / name / "service.yaml"

    def list_service_names(self) -> list[str]:
        if not self.services_dir.exists():
            return []
        return sorted(
            d.name for d in self.services_dir.iterdir()
            if d.is_dir() and (d / "service.yaml").exists()
        )

    def load_service(self, name: str) -> dict:
        path = self.service_catalog_path(name)
        if not path.exists():
            raise FileNotFoundError(f"Service '{name}' not in catalog at {path}")
        with open(path) as f:
            return yaml.safe_load(f) or {}

    def save_service(self, name: str, data: dict):
        path = self.service_catalog_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)

    def delete_service(self, name: str) -> bool:
        import shutil as _shutil
        svc_dir = self.services_dir / name
        if not svc_dir.exists():
            return False
        _shutil.rmtree(svc_dir)
        return True

    # ── Cluster CRUD ──────────────────────────────────────────────────────────

    def list_clusters(self) -> list[ClusterProfile]:
        """Return all declared cluster profiles, sorted by name."""
        return sorted(
            [self.get_cluster_profile(name) for name in self.clusters],
            key=lambda c: c.name,
        )

    def save_cluster(self, profile: ClusterProfile):
        """
        Persist a cluster profile to platform.yaml.
        Creates or overwrites the entry for profile.name.
        """
        import yaml as _yaml
        cfg_file = Path(self.config_path) if self.config_path else self.root / "platform.yaml"
        with open(cfg_file) as f:
            data = _yaml.safe_load(f) or {}

        clusters = data.setdefault("clusters", {})
        entry: dict = {"platform": profile.platform}

        if profile.is_openshift:
            entry["api_url"] = profile.api_url
            entry["context"] = profile.context
            entry["registry"] = profile.registry
            entry["helm_values_suffix"] = profile.helm_values_suffix
        else:  # aws
            entry["region"]       = profile.region
            entry["cluster_name"] = profile.cluster_name
            entry["registry"]     = profile.registry
            entry["helm_values_suffix"] = profile.helm_values_suffix

        clusters[profile.name] = entry
        # Also refresh in-memory
        self.clusters[profile.name] = entry

        with open(cfg_file, "w") as f:
            _yaml.dump(data, f, default_flow_style=False, allow_unicode=True,
                       sort_keys=False)

    def delete_cluster(self, name: str) -> bool:
        """
        Remove a cluster profile from platform.yaml.
        Returns True if it existed, False if not found.
        """
        import yaml as _yaml
        cfg_file = Path(self.config_path) if self.config_path else self.root / "platform.yaml"
        with open(cfg_file) as f:
            data = _yaml.safe_load(f) or {}

        if name not in data.get("clusters", {}):
            return False

        del data["clusters"][name]
        self.clusters.pop(name, None)

        with open(cfg_file, "w") as f:
            _yaml.dump(data, f, default_flow_style=False, allow_unicode=True,
                       sort_keys=False)
        return True

    def cluster_in_use(self, name: str) -> list[str]:
        """
        Return the list of environment names that reference this cluster.
        Used to warn before deleting a cluster profile.
        """
        in_use = []
        for env_name in self.list_envs():
            try:
                data = self.load_versions(env_name)
                if data.get("_meta", {}).get("cluster") == name:
                    in_use.append(env_name)
            except Exception:
                pass
        return in_use
