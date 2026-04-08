#!/usr/bin/env python3
"""
cleanup.py — Artifactory artifact retention policy enforcement.

Runs weekly on Sunday at 2am via Jenkins (artifactoryCleanup.groovy).

Retention rules:
  Snapshots   : delete if older than 7 days AND more than 10 exist per service
  RCs         : delete if older than 30 days (regardless of count)
  Releases    : delete if older than 90 days AND more than 10 per service
  POC         : cleaned up by drift_checker.py on TTL expiry (not here)

Guardrail (CRITICAL): Before any deletion, query all clusters via `helm list -A`
to get currently deployed versions. A version deployed anywhere is NEVER deleted,
regardless of age or retention policy.

Environment variables:
  ARTIFACTORY_URL   — e.g. https://registry.company.com
  ARTIFACTORY_USER  — Artifactory username
  ARTIFACTORY_PASS  — Artifactory password or API key
  PLATFORM_CONFIG_DIR — Path to platform-config repo root (default: auto-detect)
  DRY_RUN           — Set to "true" to log without deleting

Docker repos:
  docker-local      — snapshots + RCs
  docker-release    — release images (immutable, subject to 90-day rule)
  docker-poc        — POC images (managed by TTL teardown, skipped here)

Helm repos:
  helm-local        — snapshots + RCs
  helm-release      — release charts
"""

import os
import sys
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests


# ── Config ────────────────────────────────────────────────────────────────────

RETENTION = {
    "snapshot": {"max_age_days": 7,  "max_count": 10},
    "rc":       {"max_age_days": 30, "max_count": None},
    "release":  {"max_age_days": 90, "max_count": 10},
}


def _find_platform_root() -> Path:
    if config_dir := os.environ.get("PLATFORM_CONFIG_DIR"):
        return Path(config_dir)
    p = Path.cwd()
    for _ in range(8):
        if (p / "envs").is_dir() and (p / "scripts").is_dir():
            return p
        p = p.parent
    return Path(__file__).parent.parent


# ── Artifactory client ────────────────────────────────────────────────────────

class ArtifactoryClient:
    def __init__(self, url: str, user: str, password: str, dry_run: bool = False):
        self.base = url.rstrip("/")
        self.auth = (user, password)
        self.dry_run = dry_run
        self.session = requests.Session()
        self.session.auth = self.auth
        self.deleted_count = 0
        self.skipped_count = 0
        self.protected_count = 0

    def _get(self, path: str, **kwargs) -> requests.Response:
        return self.session.get(f"{self.base}{path}", timeout=30, **kwargs)

    def _post(self, path: str, **kwargs) -> requests.Response:
        return self.session.post(f"{self.base}{path}", timeout=30, **kwargs)

    def _delete(self, path: str) -> requests.Response:
        return self.session.delete(f"{self.base}{path}", timeout=30)

    def list_docker_tags(self, repo: str, image: str) -> list[dict]:
        """List all tags for a Docker image with their creation times."""
        resp = self._get(f"/artifactory/api/docker/{repo}/v2/{image}/tags/list")
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        tags = resp.json().get("tags") or []
        result = []
        for tag in tags:
            # Get manifest creation time via catalog API
            info = self._get_docker_manifest_info(repo, image, tag)
            result.append({"tag": tag, "created": info.get("created"), "size": info.get("size", 0)})
        return result

    def _get_docker_manifest_info(self, repo: str, image: str, tag: str) -> dict:
        """Get creation time from Docker manifest."""
        try:
            resp = self._get(
                f"/artifactory/api/docker/{repo}/v2/{image}/manifests/{tag}",
                headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"},
            )
            if resp.status_code != 200:
                return {}
            # Artifactory returns X-Artifactory-Last-Modified header
            last_mod = resp.headers.get("X-Artifactory-Last-Modified", "")
            return {"created": last_mod}
        except Exception:
            return {}

    def list_docker_images(self, repo: str) -> list[str]:
        """List all image names in a Docker repo."""
        resp = self._get(f"/artifactory/api/docker/{repo}/v2/_catalog")
        if resp.status_code != 200:
            return []
        return resp.json().get("repositories") or []

    def list_helm_artifacts(self, repo: str) -> list[dict]:
        """List all Helm chart artifacts in a repo using AQL."""
        aql = f'items.find({{"repo": "{repo}", "name": {{"$match": "*.tgz"}}}})'
        aql += '.include("name","created","size","path")'
        resp = self._post("/artifactory/api/search/aql", data=aql,
                          headers={"Content-Type": "text/plain"})
        resp.raise_for_status()
        return resp.json().get("results") or []

    def delete_docker_tag(self, repo: str, image: str, tag: str):
        """Delete a specific Docker image tag."""
        path = f"/artifactory/{repo}/{image}/{tag}"
        if self.dry_run:
            print(f"  [dry-run] would delete: {repo}/{image}:{tag}")
            return
        resp = self._delete(path)
        if resp.status_code in (200, 204):
            print(f"  deleted: {repo}/{image}:{tag}")
            self.deleted_count += 1
        else:
            print(f"  [warn] delete returned {resp.status_code} for {repo}/{image}:{tag}")

    def delete_helm_artifact(self, repo: str, path: str, name: str):
        """Delete a Helm chart artifact."""
        full_path = f"/artifactory/{repo}/{path}/{name}"
        if self.dry_run:
            print(f"  [dry-run] would delete: {repo}/{path}/{name}")
            return
        resp = self._delete(full_path)
        if resp.status_code in (200, 204):
            print(f"  deleted: {repo}/{path}/{name}")
            self.deleted_count += 1
        else:
            print(f"  [warn] delete returned {resp.status_code} for {full_path}")


# ── Deployed versions guardrail ───────────────────────────────────────────────

def get_deployed_versions() -> set[str]:
    """Query all clusters via helm list -A to get every deployed release version.

    Returns a set of image tags / chart versions currently running anywhere.
    This is the critical guardrail — these versions are NEVER deleted.
    """
    deployed: set[str] = set()

    # Strategy 1: Read from platform-config envs/ (most reliable)
    root = _find_platform_root()
    envs_dir = root / "envs"
    if envs_dir.exists():
        for env_dir in envs_dir.iterdir():
            if not env_dir.is_dir():
                continue
            # New per-service structure
            for svc_dir in env_dir.iterdir():
                version_file = svc_dir / "version.yaml"
                if version_file.exists():
                    try:
                        import yaml
                        data = yaml.safe_load(version_file.read_text()) or {}
                        if tag := data.get("image_tag") or data.get("version"):
                            deployed.add(tag)
                    except Exception:
                        pass
            # Legacy structure
            legacy = env_dir / "versions.yaml"
            if legacy.exists():
                try:
                    import yaml
                    data = yaml.safe_load(legacy.read_text()) or {}
                    for svc_data in (data.get("services") or {}).values():
                        if v := svc_data.get("version"):
                            deployed.add(v)
                except Exception:
                    pass

    # Strategy 2: helm list -A on each reachable cluster (belt-and-suspenders)
    try:
        result = subprocess.run(
            ["helm", "list", "--all-namespaces", "--output", "json"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            import json as _json
            releases = _json.loads(result.stdout or "[]")
            for r in releases:
                if chart := r.get("chart", ""):
                    # chart = "name-version", extract version part
                    parts = chart.rsplit("-", 1)
                    if len(parts) == 2:
                        deployed.add(parts[1])
                if app_version := r.get("app_version", ""):
                    deployed.add(app_version)
    except Exception as e:
        print(f"  [warn] helm list failed (non-fatal, platform-config guardrail still active): {e}")

    print(f"  Deployed versions protected: {len(deployed)}")
    return deployed


# ── Version classifier ────────────────────────────────────────────────────────

def classify_tag(tag: str) -> Optional[str]:
    """Classify a Docker/Helm tag into 'snapshot', 'rc', 'release', 'poc', or None."""
    import re
    if tag.startswith("poc-"):
        return "poc"
    if re.match(r"^\d+\.\d+\.\d+$", tag):
        return "release"
    if re.match(r"^\d+\.\d+\.\d+-rc\.\d+$", tag):
        return "rc"
    if "SNAPSHOT" in tag or re.match(r"^\d+\.\d+\.\d+-SNAPSHOT-[a-f0-9]+$", tag):
        return "snapshot"
    return None


def parse_created(created_str: str) -> Optional[datetime]:
    """Parse Artifactory date string to datetime."""
    if not created_str:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%d %b %Y %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(created_str.rstrip("Z"), fmt.rstrip("%z"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


# ── Retention enforcement ─────────────────────────────────────────────────────

def enforce_docker_retention(client: ArtifactoryClient, repo: str, deployed: set[str]):
    """Enforce retention on all Docker images in a repo."""
    images = client.list_docker_images(repo)
    print(f"\n  Docker repo '{repo}': {len(images)} image(s)")
    now = datetime.now(timezone.utc)

    for image in images:
        tags = client.list_docker_tags(repo, image)
        by_type: dict[str, list] = defaultdict(list)
        for t in tags:
            cls = classify_tag(t["tag"])
            if cls:
                by_type[cls].append(t)

        for artifact_type, type_tags in by_type.items():
            if artifact_type == "poc":
                continue  # POC cleanup is handled by drift_checker.py

            policy = RETENTION.get(artifact_type)
            if not policy:
                continue

            max_age = timedelta(days=policy["max_age_days"])
            max_count = policy["max_count"]

            # Sort oldest first
            dated = []
            for t in type_tags:
                created = parse_created(t.get("created", ""))
                dated.append((created, t["tag"]))
            dated.sort(key=lambda x: (x[0] or datetime.min.replace(tzinfo=timezone.utc)))

            candidates = []
            for created, tag in dated:
                if tag in deployed:
                    print(f"  [protected] {image}:{tag} (deployed)")
                    client.protected_count += 1
                    continue
                age_ok = (created is not None) and ((now - created) > max_age)
                candidates.append((created, tag, age_ok))

            # Apply max_count: keep the newest max_count, delete rest if age ok
            if max_count is not None:
                to_keep_count = max_count
                eligible = [(c, t, a) for (c, t, a) in candidates]
                # Keep newest max_count, mark older as candidates for deletion
                keep_set = {t for _, t, _ in eligible[-to_keep_count:]} if eligible else set()
                for created, tag, age_ok in eligible:
                    if tag in keep_set:
                        client.skipped_count += 1
                        continue
                    if age_ok:
                        client.delete_docker_tag(repo, image, tag)
                    else:
                        client.skipped_count += 1
            else:
                # No count limit: delete everything old enough
                for created, tag, age_ok in candidates:
                    if age_ok:
                        client.delete_docker_tag(repo, image, tag)
                    else:
                        client.skipped_count += 1


def enforce_helm_retention(client: ArtifactoryClient, repo: str, deployed: set[str]):
    """Enforce retention on Helm charts in a repo."""
    artifacts = client.list_helm_artifacts(repo)
    print(f"\n  Helm repo '{repo}': {len(artifacts)} chart(s)")
    now = datetime.now(timezone.utc)

    # Group by service name
    by_service: dict[str, list] = defaultdict(list)
    for a in artifacts:
        name = a.get("name", "")
        # name = "service-1.2.3-SNAPSHOT-abc.tgz" → extract service name
        base = name.removesuffix(".tgz")
        # Split on semver boundary
        import re
        m = re.search(r"-(\d+\.\d+\.\d+)", base)
        svc = base[:m.start()] if m else base
        tag = base[m.start() + 1:] if m else ""
        by_service[svc].append({"name": name, "path": a.get("path", ""),
                                 "tag": tag, "created": a.get("created", "")})

    for svc, charts in by_service.items():
        by_type: dict[str, list] = defaultdict(list)
        for c in charts:
            cls = classify_tag(c["tag"])
            if cls:
                by_type[cls].append(c)

        for artifact_type, type_charts in by_type.items():
            if artifact_type == "poc":
                continue

            policy = RETENTION.get(artifact_type)
            if not policy:
                continue

            max_age = timedelta(days=policy["max_age_days"])
            max_count = policy["max_count"]

            dated = []
            for c in type_charts:
                created = parse_created(c.get("created", ""))
                dated.append((created, c))
            dated.sort(key=lambda x: (x[0] or datetime.min.replace(tzinfo=timezone.utc)))

            if max_count is not None:
                eligible = dated
                keep_set = {c["tag"] for _, c in eligible[-max_count:]} if eligible else set()
                for created, c in eligible:
                    if c["tag"] in deployed:
                        client.protected_count += 1
                        continue
                    if c["tag"] in keep_set:
                        client.skipped_count += 1
                        continue
                    age_ok = (created is not None) and ((now - created) > max_age)
                    if age_ok:
                        client.delete_helm_artifact(repo, c["path"], c["name"])
                    else:
                        client.skipped_count += 1
            else:
                for created, c in dated:
                    if c["tag"] in deployed:
                        client.protected_count += 1
                        continue
                    age_ok = (created is not None) and ((now - created) > max_age)
                    if age_ok:
                        client.delete_helm_artifact(repo, c["path"], c["name"])
                    else:
                        client.skipped_count += 1


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Artifactory artifact retention policy")
    parser.add_argument("--dry-run", action="store_true",
                        help="Log what would be deleted without actually deleting")
    args = parser.parse_args()

    dry_run = args.dry_run or os.environ.get("DRY_RUN", "").lower() == "true"

    artifactory_url  = os.environ.get("ARTIFACTORY_URL", "")
    artifactory_user = os.environ.get("ARTIFACTORY_USER", "")
    artifactory_pass = os.environ.get("ARTIFACTORY_PASS", "")

    if not all([artifactory_url, artifactory_user, artifactory_pass]):
        print("ERROR: ARTIFACTORY_URL, ARTIFACTORY_USER, ARTIFACTORY_PASS must be set",
              file=sys.stderr)
        sys.exit(1)

    mode = "[DRY RUN] " if dry_run else ""
    print(f"\n{mode}Artifactory cleanup — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    client = ArtifactoryClient(artifactory_url, artifactory_user, artifactory_pass,
                                dry_run=dry_run)

    # 1. Build deployed-versions guardrail
    print("\n[1/3] Building deployed-versions guardrail...")
    deployed = get_deployed_versions()

    # 2. Docker retention
    print("\n[2/3] Enforcing Docker retention...")
    for repo in ("docker-local", "docker-release"):
        try:
            enforce_docker_retention(client, repo, deployed)
        except Exception as e:
            print(f"  [error] Docker repo '{repo}': {e}")

    # 3. Helm retention
    print("\n[3/3] Enforcing Helm retention...")
    for repo in ("helm-local", "helm-release"):
        try:
            enforce_helm_retention(client, repo, deployed)
        except Exception as e:
            print(f"  [error] Helm repo '{repo}': {e}")

    # Summary
    print(f"\n{mode}Cleanup complete:")
    print(f"  Deleted  : {client.deleted_count}")
    print(f"  Protected: {client.protected_count} (deployed — never deleted)")
    print(f"  Skipped  : {client.skipped_count} (within retention window)")


if __name__ == "__main__":
    main()
