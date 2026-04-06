"""
cluster_manager.py — Manage cluster profiles in platform.yaml.

A cluster profile tells the platform:
  - Which technology runs it (openshift | aws)
  - Where to connect (API URL / context for OpenShift, region + EKS name for AWS)
  - Which container registry to use
  - Which Helm values suffix to apply (resolves to helm/values-{suffix}.yaml)

Profiles are stored in platform.yaml under `clusters:` and are referenced
by environments via their `_meta.cluster` field.
"""

from __future__ import annotations

import json
from dataclasses import asdict

from config import PlatformConfig, ClusterProfile
from output import out, step, success, warn, error_exit


# ── Field definitions per platform ────────────────────────────────────────────

OPENSHIFT_FIELDS = [
    ("api_url",            "API server URL",
     "https://api.my-cluster.example.com:6443"),
    ("context",            "kubeconfig context name",
     "my-cluster/system:admin"),
    ("registry",           "Container registry",
     "registry.internal"),
    ("helm_values_suffix", "Helm values file suffix (values-{suffix}.yaml)",
     "dev"),
]

AWS_FIELDS = [
    ("region",             "AWS region",
     "eu-west-1"),
    ("cluster_name",       "EKS cluster name (used with aws eks update-kubeconfig)",
     "my-eks-cluster"),
    ("registry",           "ECR registry URL",
     "123456789.dkr.ecr.eu-west-1.amazonaws.com"),
    ("helm_values_suffix", "Helm values file suffix (values-{suffix}.yaml)",
     "dev"),
]


# ── Manager ───────────────────────────────────────────────────────────────────

class ClusterManager:
    def __init__(self, cfg: PlatformConfig, json_output: bool = False):
        self.cfg = cfg
        self.json_output = json_output

    # ── list ─────────────────────────────────────────────────────────────────

    def list_clusters(self):
        clusters = self.cfg.list_clusters()
        if self.json_output:
            print(json.dumps([self._profile_to_dict(c) for c in clusters], indent=2))
            return
        if not clusters:
            out("No cluster profiles defined. Use 'cluster add' to create one.")
            return

        col_w = [22, 12, 28, 28]
        header = ["Cluster", "Platform", "Endpoint / Region", "Registry"]
        rows = []
        for c in clusters:
            if c.is_openshift:
                endpoint = c.api_url or c.context or "—"
            else:
                endpoint = f"{c.region} / {c.cluster_name}" if c.region else "—"
            rows.append([c.name, c.platform, endpoint[:28], c.registry[:28]])
        self._print_table(header, rows, col_w)

    # ── info ─────────────────────────────────────────────────────────────────

    def info(self, name: str):
        if name not in self.cfg.clusters:
            error_exit(f"Cluster '{name}' not found. Use 'cluster list' to see known clusters.")
        profile = self.cfg.get_cluster_profile(name)

        if self.json_output:
            print(json.dumps(self._profile_to_dict(profile), indent=2))
            return

        in_use = self.cfg.cluster_in_use(name)
        print(f"\n  Cluster  : {profile.name}")
        print(f"  Platform : {profile.platform}")
        print(f"  Registry : {profile.registry}")
        print(f"  Helm sfx : values-{profile.helm_values_suffix}.yaml")
        if profile.is_openshift:
            print(f"  API URL  : {profile.api_url or '—'}")
            print(f"  Context  : {profile.context or '—'}")
        else:
            print(f"  Region   : {profile.region or '—'}")
            print(f"  EKS name : {profile.cluster_name or '—'}")
        if in_use:
            print(f"\n  Used by  : {', '.join(in_use)}")
        else:
            print(f"\n  Used by  : (no environments)")
        print()

    # ── add ──────────────────────────────────────────────────────────────────

    def add(
        self,
        name: str,
        platform: str,
        # OpenShift fields
        api_url: str = "",
        context: str = "",
        # AWS fields
        region: str = "",
        cluster_name: str = "",
        # Shared
        registry: str = "",
        helm_values_suffix: str = "",
    ):
        if name in self.cfg.clusters:
            error_exit(
                f"Cluster '{name}' already exists. "
                "Use 'cluster remove' first if you want to replace it."
            )

        # Default registry from platform-level registries config
        if not registry:
            registry = self.cfg.registries.get(platform, self.cfg.registry)

        # Default helm suffix from last segment of name (e.g. "openshift-dev" → "dev")
        if not helm_values_suffix:
            helm_values_suffix = name.split("-")[-1]

        profile = ClusterProfile(
            name=name,
            platform=platform,
            registry=registry,
            helm_values_suffix=helm_values_suffix,
            api_url=api_url,
            context=context,
            region=region,
            cluster_name=cluster_name,
        )

        self.cfg.save_cluster(profile)
        success(f"Cluster '{name}' added ({platform}).")
        self.info(name)

    # ── remove ───────────────────────────────────────────────────────────────

    def remove(self, name: str, force: bool = False):
        if name not in self.cfg.clusters:
            error_exit(f"Cluster '{name}' not found.")

        in_use = self.cfg.cluster_in_use(name)
        if in_use and not force:
            error_exit(
                f"Cluster '{name}' is referenced by environment(s): "
                f"{', '.join(in_use)}.\n"
                "  Update or remove those environments first, "
                "or use --force to remove the profile anyway."
            )
        if in_use and force:
            warn(f"Force-removing cluster '{name}' which is still used by: {', '.join(in_use)}")

        self.cfg.delete_cluster(name)
        success(f"Cluster '{name}' removed from platform.yaml.")

    # ── helpers ───────────────────────────────────────────────────────────────

    def _profile_to_dict(self, c: ClusterProfile) -> dict:
        d = {
            "name":               c.name,
            "platform":           c.platform,
            "registry":           c.registry,
            "helm_values_suffix": c.helm_values_suffix,
            "in_use":             self.cfg.cluster_in_use(c.name),
        }
        if c.is_openshift:
            d["api_url"] = c.api_url
            d["context"] = c.context
        else:
            d["region"]       = c.region
            d["cluster_name"] = c.cluster_name
        return d

    def _print_table(self, header, rows, col_widths):
        def fmt(row):
            return "  " + "  ".join(str(v)[:w].ljust(w) for v, w in zip(row, col_widths))
        sep = "  " + "  ".join("-" * w for w in col_widths)
        print()
        print(fmt(header))
        print(sep)
        for row in rows:
            print(fmt(row))
        print()
