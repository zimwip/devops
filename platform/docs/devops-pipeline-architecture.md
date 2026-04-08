# DevOps Pipeline Architecture — Python + Artifactory + Drift Detection

## Overview

The goal is to maintain a **portable, explicit, and non-intrusive** deployment approach, with no dependency on a cluster-specific CD tool (such as ArgoCD), while guaranteeing consistency between the desired state (Git) and the actual state (OpenShift / future AWS).

```
┌─────────────────────────────────────────────────────────────────┐
│                        DEV / CI                                 │
│                                                                 │
│   Service Repo          Jenkins Build         Artifactory       │
│  ┌──────────┐          ┌──────────┐          ┌──────────────┐  │
│  │ src/     │─ build ─▶│ pipeline │─ push ──▶│ docker imgs  │  │
│  │ helm/    │─ pack ──▶│          │─ push ──▶│ helm charts  │  │
│  └──────────┘          └──────────┘          └──────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                                                        │
                                                        ▼
┌─────────────────────────────────────────────────────────────────┐
│                     PLATFORM REPO (Git)                         │
│                                                                 │
│   envs/                                                         │
│   ├── dev/my-service/                                           │
│   │   ├── version.yaml   ← desired version                     │
│   │   └── values.yaml    ← env config                          │
│   └── prod/my-service/                                          │
│       ├── version.yaml                                          │
│       └── values.yaml                                           │
└─────────────────────────────────────────────────────────────────┘
                │                          │
        deploy  │                  poll    │
        (push)  │                  (pull)  │
                ▼                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Python Scripts                                │
│                                                                 │
│   deploy.py          ◀──── triggered by Git commit/tag         │
│   drift_checker.py   ◀──── scheduled polling (cron)            │
└─────────────────────────────────────────────────────────────────┘
                │
                ▼
┌─────────────────────────────────────────────────────────────────┐
│              Cluster (OpenShift / EKS / other)                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 1. Versioning Strategy — version.txt

### 1.1 Principle

Every service repo contains a `version.txt` file at its root. This file holds the **next target version** and is the single source of truth for all version-related logic across the pipeline.

```
# version.txt — committed at repo root
1.2.0
```

The developer updates this file manually when starting work toward a new version. Jenkins reads it at build time and uses it to compute the artifact tag depending on the current branch.

### 1.2 Version resolution by branch

| Branch | Version source | Artifact tag |
|---|---|---|
| `feature/*` | not published | — |
| `develop` | `version.txt` + Git SHA | `1.2.0-SNAPSHOT-a3f1c2d` |
| `release/1.2.0` | branch name + build number | `1.2.0-rc.3` |
| `main` + tag `v1.2.0` | Git tag | `1.2.0` |
| `hotfix/1.2.1` | `version.txt` + Git tag | `1.2.1` |
| `poc/mypoc` | poc prefix + name | `poc-mypoc` |

### 1.3 Validation rules per branch

Jenkins runs `validate_version.py` as the **very first stage** — before unit tests, before Sonar, before anything. An incoherent version stops the pipeline immediately.

| Branch | Rule |
|---|---|
| All branches | `version.txt` must exist and match semver `X.Y.Z` |
| `release/X.Y.Z` | `version.txt` must equal the version in the branch name |
| `main` + Git tag | `version.txt` must equal the Git tag (without `v` prefix) |
| `main` without tag | pipeline stops — no release build triggered |

### 1.4 validate_version.py

```python
# scripts/validate_version.py
import re, sys, subprocess

SEMVER = re.compile(r'^\d+\.\d+\.\d+$')

def read_version_file() -> str:
    try:
        return open("version.txt").read().strip()
    except FileNotFoundError:
        fail("version.txt not found — create the file at repo root")

def get_branch() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"]
    ).decode().strip()

def get_git_tag() -> str | None:
    result = subprocess.run(
        ["git", "describe", "--tags", "--exact-match"],
        capture_output=True, text=True
    )
    return result.stdout.strip().lstrip("v") if result.returncode == 0 else None

def fail(msg: str):
    print(f"[VERSION ERROR] {msg}")
    sys.exit(1)

def validate():
    version = read_version_file()
    branch  = get_branch()
    tag     = get_git_tag()

    # Rule 0 — semver format required on all branches
    if not SEMVER.match(version):
        fail(f"version.txt '{version}' is not valid semver (e.g. 1.2.0)")

    # Rule 1 — release/* : branch name must match version.txt
    if branch.startswith("release/"):
        branch_version = branch.replace("release/", "")
        if version != branch_version:
            fail(
                f"Mismatch: branch '{branch}' "
                f"but version.txt = '{version}' "
                f"(expected: '{branch_version}')"
            )

    # Rule 2 — main with tag : Git tag must match version.txt
    if branch == "main" and tag:
        if version != tag:
            fail(
                f"Mismatch: Git tag 'v{tag}' "
                f"but version.txt = '{version}' "
                f"(expected: '{tag}')"
            )

    # Rule 3 — main without tag : no release build
    if branch == "main" and not tag:
        print("[VERSION] main branch without tag — skipping release build")
        sys.exit(0)

    print(f"[VERSION OK] {version} on '{branch}'")

if __name__ == "__main__":
    validate()
```

### 1.5 Developer workflow

```bash
# Starting a new iteration on develop
echo "1.2.0" > version.txt
git add version.txt
git commit -m "chore: bump version to 1.2.0"

# Creating the release branch — version.txt already at 1.2.0
git checkout -b release/1.2.0
# Jenkins validates: branch release/1.2.0 == version.txt 1.2.0 ✅

# After merge to main
git tag v1.2.0
git push --tags
# Jenkins validates: tag v1.2.0 == version.txt 1.2.0 ✅

# Starting next iteration
git checkout develop
echo "1.3.0" > version.txt
git commit -m "chore: bump version to 1.3.0"
```

### 1.6 Hotfix edge case

When a hotfix is needed on a past version while develop has already moved forward:

```
main (tag v1.2.0)
  └── hotfix/1.2.1
        version.txt = 1.2.1
        → merge to main + tag v1.2.1
        → Jenkins validates: tag v1.2.1 == version.txt 1.2.1 ✅
        (no coherence check with develop — hotfix/* is not release/*)
```

---

## 2. Artifactory — Artifact Source of Truth

### 2.1 Published artifacts

Each service publishes **two artifacts** on every Jenkins build:

| Artifact | Type | Artifactory Repository | Example |
|---|---|---|---|
| Docker image | OCI / Docker | `docker-local` | `my-service:1.2.0` |
| Helm Chart | Helm / OCI | `helm-local` | `my-service-1.2.0.tgz` |

### 2.2 Naming conventions

```
# Docker image
registry.artifactory.company.com/docker-local/my-service:1.2.0

# Helm Chart (OCI format — recommended)
oci://registry.artifactory.company.com/helm-local/my-service
  → version: 1.2.0

# Helm Chart (classic repo format)
https://artifactory.company.com/artifactory/helm-local
  → my-service-1.2.0.tgz
```

### 2.3 Versioning strategy in Artifactory

```
develop branch  →  snapshot  →  my-service:1.2.0-SNAPSHOT-a3f1c2d
release branch  →  RC        →  my-service:1.2.0-rc.3
main tag v1.2.0 →  release   →  my-service:1.2.0
poc/mypoc       →  poc       →  my-service:poc-mypoc  (docker-poc repo)
```

Snapshots are automatically purged by retention policy.
Releases are immutable — a tag cannot be overwritten.

### 2.4 Jenkinsfile — Helm chart publication (OCI)

```groovy
stage('Publish Helm Chart') {
    steps {
        sh """
            helm registry login registry.artifactory.company.com \
                -u ${ARTIFACTORY_USER} -p ${ARTIFACTORY_TOKEN}
            helm package ./helm --version ${VERSION}
            helm push my-service-${VERSION}.tgz \
                oci://registry.artifactory.company.com/helm-local
        """
    }
}
```

### 2.5 Release promotion — retag without rebuild

At promotion time, the snapshot image is **retagged** in Artifactory — no Docker rebuild. What was tested is exactly what gets deployed, bit for bit.

```python
def promote_artifact(service: str, snapshot_tag: str, release_tag: str):
    requests.post(
        f"{ARTIFACTORY_URL}/api/docker/docker-local/v2/promote",
        json={
            "targetRepo":       "docker-local",
            "dockerRepository": service,
            "tag":              snapshot_tag,
            "targetTag":        release_tag,
            "copy":             True   # keeps snapshot for traceability
        },
        headers={"Authorization": f"Bearer {ARTIFACTORY_TOKEN}"}
    )
```

---

## 3. Artifactory Cleanup — Retention Policy

### 3.1 Retention rules by artifact type

| Type | Retention policy | Deletion rule |
|---|---|---|
| `snapshot` (develop) | 7 days or last 10 | Purged automatically |
| `poc-*` | POC env TTL | Deleted on env expiry |
| `rc` (release/) | 30 days | Deleted after prod release |
| `release` (main tag) | 90 days / last 10 | Never if deployed in prod |

### 3.2 Absolute guardrail — protect deployed versions

Before any deletion, the script queries all clusters to protect currently deployed versions:

```python
import subprocess, json

def get_all_deployed_versions() -> dict[str, set]:
    """Returns { service_name: {version1, version2, ...} } across all envs."""
    protected = {}
    result = subprocess.run(
        ["helm", "list", "-A", "-o", "json"],
        capture_output=True, text=True
    )
    for r in json.loads(result.stdout):
        parts   = r["chart"].rsplit("-", 1)
        service = parts[0]
        version = parts[1] if len(parts) == 2 else "unknown"
        protected.setdefault(service, set()).add(version)
    return protected

def safe_to_delete(service: str, version: str, protected: dict) -> bool:
    return version not in protected.get(service, set())
```

### 3.3 cleanup.py — Weekly job

```python
import requests
from datetime import datetime, timedelta

ARTIFACTORY_URL   = "https://artifactory.company.com/artifactory"
ARTIFACTORY_TOKEN = "..."

RETENTION_POLICIES = {
    "snapshot": {"max_days": 7,  "max_count": 10},
    "poc":      {"max_days": 3,  "max_count": 999},
    "rc":       {"max_days": 30, "max_count": 5},
    "release":  {"max_days": 90, "max_count": 10},
}

def run_cleanup():
    protected = get_all_deployed_versions()
    for repo, policy in [
        ("docker-poc",   RETENTION_POLICIES["poc"]),
        ("docker-local", RETENTION_POLICIES["snapshot"]),
        ("helm-local",   RETENTION_POLICIES["release"]),
    ]:
        artifacts = list_artifacts(repo, "*")
        cutoff    = datetime.now() - timedelta(days=policy["max_days"])
        for artifact in artifacts:
            service, version = parse_artifact_name(artifact["uri"])
            created_at = datetime.fromisoformat(artifact["lastModified"])
            if created_at < cutoff and safe_to_delete(service, version, protected):
                delete_artifact(repo, artifact["uri"])

if __name__ == "__main__":
    run_cleanup()
```

### 3.4 Jenkins cleanup job

```groovy
pipeline {
    triggers { cron('0 2 * * 0') }  // every Sunday at 2am
    stages {
        stage('Cleanup Artifactory') {
            steps {
                withKubeConfig([credentialsId: 'openshift-kubeconfig']) {
                    sh 'python scripts/cleanup.py'
                }
            }
        }
    }
}
```

---

## 4. Platform Repo — Structure and Desired State

### 4.1 Repository structure

```
platform-repo/
├── envs/
│   ├── dev/
│   │   ├── my-service/
│   │   │   ├── version.yaml
│   │   │   └── values.yaml
│   │   └── other-service/
│   │       ├── version.yaml
│   │       └── values.yaml
│   ├── staging/
│   │   └── my-service/
│   │       ├── version.yaml
│   │       └── values.yaml
│   ├── prod/
│   │   └── my-service/
│   │       ├── version.yaml
│   │       └── values.yaml
│   └── poc-mypoc/                  ← POC environment (see section 8)
│       ├── poc.yaml
│       └── my-service/
│           ├── version.yaml
│           └── values.yaml
├── scripts/
│   ├── deploy.py
│   ├── drift_checker.py
│   ├── cleanup.py
│   ├── validate_version.py
│   └── utils/
│       ├── helm_client.py
│       └── notifier.py
└── config/
    └── environments.yaml           ← kubeconfig, namespaces, clusters
```

### 4.2 version.yaml

```yaml
service: my-service
chart_version: "1.2.0"
image_tag: "1.2.0"
chart_repo: "oci://registry.artifactory.company.com/helm-local"
```

### 4.3 values.yaml

```yaml
replicaCount: 2
image:
  repository: registry.artifactory.company.com/docker-local/my-service
  tag: "1.2.0"   # dynamically overridden by deploy script
resources:
  requests:
    memory: "256Mi"
    cpu: "100m"
ingress:
  host: my-service.dev.company.com
```

### 4.4 environments.yaml

```yaml
environments:
  dev:
    kubeconfig: ~/.kube/openshift-dev
    type: openshift
  staging:
    kubeconfig: ~/.kube/openshift-staging
    type: openshift
  prod:
    kubeconfig: ~/.kube/openshift-prod
    type: openshift
  prod-aws:
    kubeconfig: ~/.kube/eks-prod
    type: eks
```

---

## 5. Deployment Script — deploy.py

```python
import subprocess, yaml, sys, os
from pathlib import Path

ARTIFACTORY_REGISTRY = "oci://registry.artifactory.company.com/helm-local"

def load_env_config(env: str) -> dict:
    cfg = yaml.safe_load(open("config/environments.yaml"))
    return cfg["environments"][env]

def set_kube_context(env: str):
    os.environ["KUBECONFIG"] = load_env_config(env)["kubeconfig"]

def load_config(env: str, service: str):
    base    = Path(f"envs/{env}/{service}")
    version = yaml.safe_load((base / "version.yaml").read_text())
    return version, base / "values.yaml"

def validate_deployment(env: str, image_tag: str):
    is_poc_image = str(image_tag).startswith("poc-")
    is_poc_env   = env.startswith("poc-")
    if is_poc_image and not is_poc_env:
        raise ValueError(f"[GUARDRAIL] POC image '{image_tag}' rejected on env '{env}'")
    if is_poc_env and is_poc_image:
        poc_name     = env.removeprefix("poc-")
        expected_tag = f"poc-{poc_name}"
        if image_tag != expected_tag:
            raise ValueError(
                f"[GUARDRAIL] Tag '{image_tag}' does not match env '{env}' "
                f"(expected: '{expected_tag}')"
            )

def deploy(env: str, service: str, dry_run: bool = False):
    set_kube_context(env)
    version_cfg, values_path = load_config(env, service)
    validate_deployment(env, version_cfg["image_tag"])

    cmd = [
        "helm", "upgrade", "--install", service,
        f"{ARTIFACTORY_REGISTRY}/{service}",
        "--version",        version_cfg["chart_version"],
        "--namespace",      f"{env}-{service}",
        "--create-namespace",
        "--values",         str(values_path),
        "--set",            f"image.tag={version_cfg['image_tag']}",
        "--atomic",
        "--timeout",        "5m",
    ]
    if dry_run:
        cmd.append("--dry-run")

    print(f"[DEPLOY] {service} v{version_cfg['chart_version']} → {env}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[ERROR] {result.stderr}")
        sys.exit(1)
    print(f"[OK] {service} deployed successfully")

if __name__ == "__main__":
    deploy(sys.argv[1], sys.argv[2])
```

---

## 6. Drift Detection — drift_checker.py

### 6.1 Principle

The drift checker runs on a **regular polling schedule** and compares:
- **Desired state**: `version.yaml` in the platform repo
- **Actual state**: active Helm release in the cluster

```
┌─────────────────┐        ┌──────────────────────┐
│  Platform Repo  │        │   Cluster            │
│  version.yaml   │──────▶ │   helm list          │
│  chart: 1.2.0   │  diff  │   deployed: 1.1.0 ←── DRIFT DETECTED
└─────────────────┘        └──────────────────────┘
         │
         ▼
  Slack notification + log
  (optional: auto-remediation on dev)
```

### 6.2 drift_checker.py

```python
import subprocess, yaml, json
from pathlib import Path
from utils.notifier import notify_slack

STANDARD_ENVS = ["dev", "staging", "prod"]

def get_deployed_version(service: str, namespace: str) -> str | None:
    result = subprocess.run(
        ["helm", "list", "-n", namespace, "-o", "json"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    for release in json.loads(result.stdout):
        if release["name"] == service:
            return release["chart"].split("-")[-1]
    return None

def get_desired_version(env: str, service: str) -> str:
    path = Path(f"envs/{env}/{service}/version.yaml")
    return yaml.safe_load(path.read_text())["chart_version"]

def check_drift(env: str, service: str) -> dict:
    desired  = get_desired_version(env, service)
    deployed = get_deployed_version(service, f"{env}-{service}")
    return {
        "env": env, "service": service,
        "desired": desired, "deployed": deployed,
        "drift": deployed != desired,
    }

def run_all():
    all_envs = STANDARD_ENVS + [
        p.name for p in Path("envs").iterdir()
        if p.name.startswith("poc-")
    ]
    drifts = []
    for env in all_envs:
        env_path = Path(f"envs/{env}")
        if not env_path.exists():
            continue
        for svc_path in env_path.iterdir():
            if not svc_path.is_dir() or svc_path.name == "poc.yaml":
                continue
            result = check_drift(env, svc_path.name)
            if result["drift"]:
                drifts.append(result)
                print(f"[DRIFT] {result['env']}/{result['service']} "
                      f"desired={result['desired']} deployed={result['deployed']}")
    if drifts:
        notify_slack(drifts)
    else:
        print("[OK] No drift detected")
    check_poc_expiry()

if __name__ == "__main__":
    run_all()
```

### 6.3 Jenkins polling job

```groovy
pipeline {
    triggers { cron('H/15 * * * *') }  // every 15 minutes
    stages {
        stage('Check Drift') {
            steps {
                withKubeConfig([credentialsId: 'openshift-kubeconfig']) {
                    sh 'python scripts/drift_checker.py'
                }
            }
        }
    }
    post {
        failure {
            slackSend channel: '#ops', message: 'Drift checker job failed!'
        }
    }
}
```

### 6.4 Auto-remediation

```python
if result["drift"]:
    if AUTO_REMEDIATE and env == "dev":
        deploy(env, service)
    else:
        drifts.append(result)  # notification only
```

> Enable auto-remediation on `dev` only. On `staging` and `prod`, prefer notification + manual approval.

---

## 7. Multi-cloud Portability

The scripts are portable by design. The only thing that changes per target is the `kubeconfig` in `environments.yaml`. `helm` and `kubectl` are cloud-agnostic — no code changes required when targeting EKS, GKE, or AKS.

---

## 8. POC Environments — Full Lifecycle

### 8.1 Strict naming convention

The POC name is the central discriminator. Everything derives from it automatically:

| Element | Convention | Example |
|---|---|---|
| Git branch (service repo) | `poc/mypoc` | `poc/newauth` |
| Docker image tag | `poc-mypoc` | `poc-newauth` |
| Helm chart tag | `poc-mypoc` | `poc-newauth` |
| Artifactory repo | `docker-poc` | `docker-poc` |
| Platform repo env folder | `poc-mypoc` | `poc-newauth` |
| Cluster namespace | `poc-mypoc-{service}` | `poc-newauth-service-a` |

### 8.2 poc.yaml — Metadata and TTL

```yaml
name: newauth
description: "Testing new SSO authentication provider via Keycloak"
owner: "security-team"
created_at: "2026-04-07T10:00:00"
ttl_hours: 72
contact_slack: "#poc-newauth"
services_modified:
  - service: central-service-a
    source_branch: poc/newauth
    source_repo: https://github.com/company/central-service-a
  - service: central-service-b
    source_branch: poc/newauth
    source_repo: https://github.com/company/central-service-b
services_stable:
  - service: stable-service-x
    version: "1.4.2"
```

### 8.3 Deployment guardrails

```python
def validate_deployment(env: str, image_tag: str):
    is_poc_image = str(image_tag).startswith("poc-")
    is_poc_env   = env.startswith("poc-")
    # POC image → POC env only
    if is_poc_image and not is_poc_env:
        raise ValueError(f"[GUARDRAIL] POC image '{image_tag}' rejected on env '{env}'")
    # POC env → name must match
    if is_poc_env and is_poc_image:
        expected = f"poc-{env.removeprefix('poc-')}"
        if image_tag != expected:
            raise ValueError(f"[GUARDRAIL] Expected tag '{expected}', got '{image_tag}'")
```

### 8.4 Automated lifecycle and teardown

```python
from datetime import datetime, timedelta
import subprocess, shutil, json
from pathlib import Path

def check_poc_expiry():
    for poc_path in Path("envs").iterdir():
        if not poc_path.name.startswith("poc-"):
            continue
        cfg       = yaml.safe_load((poc_path / "poc.yaml").read_text())
        created   = datetime.fromisoformat(cfg["created_at"])
        expires   = created + timedelta(hours=cfg["ttl_hours"])
        remaining = expires - datetime.now()

        if remaining.total_seconds() < 0:
            teardown_poc(poc_path.name.removeprefix("poc-"))
        elif remaining < timedelta(hours=6):
            notify_slack(
                channel=cfg["contact_slack"],
                message=f"⚠️ POC '{poc_path.name}' expires in "
                        f"{int(remaining.total_seconds() / 3600)}h"
            )

def teardown_poc(poc_name: str):
    # 1. Delete cluster namespaces
    result = subprocess.run(
        ["kubectl", "get", "namespaces", "-o", "json"],
        capture_output=True, text=True
    )
    for ns in [
        ns["metadata"]["name"]
        for ns in json.loads(result.stdout)["items"]
        if ns["metadata"]["name"].startswith(f"poc-{poc_name}-")
    ]:
        subprocess.run(["kubectl", "delete", "namespace", ns])
        print(f"[TEARDOWN] Namespace {ns} deleted")

    # 2. Remove env folder from platform repo
    shutil.rmtree(Path(f"envs/poc-{poc_name}"))

    # 3. Delete Artifactory POC artifacts
    delete_poc_artifacts(poc_name)
```

### 8.5 Reintegration workflow via PR

When a POC is validated, service maintainers decide on reintegration:

```
poc/newauth  (central-service-a)
      │
      │  PR opened by POC team
      ▼
  develop  (central-service-a)
      │
      │  Review + merge by maintainers
      ▼
  Standard pipeline: develop → staging → prod
```

The POC dashboard provides a **"Create PR"** button that opens the `poc/mypoc → develop` comparison on GitHub/GitLab for each modified service.

---

## 9. POC Dashboard

A lightweight React interface exposed by the deployment service, showing the real-time state of all active POC environments.

**Features:**
- All active POCs with remaining TTL as a color-coded progress bar
- Services per POC: poc version vs stable version
- Direct link to the `poc/mypoc` branch for each modified service
- **"Create PR → develop"** button per service
- **"Delete env"** button with two-click confirmation
- Drift indicator per service

### 9.1 Backend API (FastAPI)

```python
from fastapi import FastAPI
from pathlib import Path
import yaml
from datetime import datetime, timedelta

app = FastAPI()

@app.get("/api/pocs")
def list_pocs():
    pocs = []
    for poc_path in Path("envs").iterdir():
        if not poc_path.name.startswith("poc-"):
            continue
        cfg     = yaml.safe_load((poc_path / "poc.yaml").read_text())
        created = datetime.fromisoformat(cfg["created_at"])
        expires = created + timedelta(hours=cfg["ttl_hours"])
        pocs.append({
            **cfg,
            "expires_at":      expires.isoformat(),
            "remaining_hours": max(0, (expires - datetime.now()).total_seconds() / 3600),
        })
    return pocs

@app.delete("/api/pocs/{poc_name}")
def delete_poc(poc_name: str):
    teardown_poc(poc_name)
    return {"status": "deleted", "poc": poc_name}
```

---

## 10. Testing Strategy

### 10.1 Philosophy

The application is primarily a frontend backed by APIs. The strategy is intentionally asymmetric — APIs are the backbone, tested exhaustively and blocking the pipeline. E2E frontend tests are a safety net on critical journeys only.

| Level | Scope | Blocks pipeline | When |
|---|---|---|---|
| Version validation | `version.txt` coherence | ✅ Yes | Every build, first |
| Unit tests | Source code | ✅ Yes | Every commit |
| Sonar Quality Gate | Source code | ✅ Yes | Every commit |
| Integration tests | APIs + business flows | ✅ Yes | Manual, on dev env |
| E2E Smoke (Playwright) | Critical UI journeys | ❌ No | Post-deploy on staging |

### 10.2 Pipeline placement

```
┌──────────────────────────────────────────────────────────────┐
│  STAGE 0 — Version Validation (blocking)                    │
│  validate_version.py                                        │
│  ❌ KO → stop immediately, nothing runs                     │
└─────────────────────────┬────────────────────────────────────┘
                          │ ✅
┌─────────────────────────▼────────────────────────────────────┐
│  STAGE 1 — Code Quality (blocking)                          │
│  Unit tests → coverage → Sonar Quality Gate                 │
│  ❌ KO → stop, nothing is published                         │
└─────────────────────────┬────────────────────────────────────┘
                          │ ✅
┌─────────────────────────▼────────────────────────────────────┐
│  STAGE 2 — Build & Publish Snapshot                         │
│  docker build → push docker-local/snapshot                  │
│  helm package → push helm-local/snapshot                    │
└─────────────────────────┬────────────────────────────────────┘
                          │ ✅
┌─────────────────────────▼────────────────────────────────────┐
│  STAGE 3 — Integration Tests (manual, blocking on trigger)  │
│  run against dev env                                        │
│  API + business flow tests                                  │
│  ❌ KO → snapshot stays unpromoted in Artifactory           │
└─────────────────────────┬────────────────────────────────────┘
                          │ ✅
┌─────────────────────────▼────────────────────────────────────┐
│  STAGE 4 — Release Promotion (no rebuild)                   │
│  retag snapshot → release in Artifactory                    │
│  same image, same SHA, new tag                              │
└─────────────────────────┬────────────────────────────────────┘
                          │
                          ▼
               Deploy dev → staging → prod
                          │
┌─────────────────────────▼────────────────────────────────────┐
│  STAGE 5 — E2E Smoke Playwright (non-blocking)              │
│  5–10 critical UI journeys on staging                       │
│  Allure report published, Slack alert on failure            │
└──────────────────────────────────────────────────────────────┘
```

### 10.3 integration-tests repository structure

```
integration-tests/
├── features/
│   ├── smoke/
│   │   └── health.feature               ← post-deploy, very fast
│   ├── api/
│   │   ├── orders.feature               ← JSON contracts, HTTP codes
│   │   ├── authentication.feature
│   │   └── catalogue.feature
│   ├── business/
│   │   ├── order_flow.feature           ← multi-service flows, co-written with PO
│   │   ├── payment.feature
│   │   └── cancellation.feature
│   └── e2e/
│       └── critical_journeys.feature    ← Playwright, staging only
├── steps/
│   ├── api_steps.py
│   ├── business_steps.py
│   └── e2e_steps.py
├── utils/
│   ├── client.py         ← reusable HTTP wrapper
│   ├── fixtures.py       ← test data setup/teardown
│   └── config.py         ← reads BASE_URL, tokens from env vars
├── reports/
├── behave.ini
└── README.md             ← contribution guide for PO / functional teams
```

### 10.4 Gherkin feature examples

#### API feature — response contract

```gherkin
# features/api/orders.feature — written by dev, validated with PO

Feature: Orders API

  Background:
    Given I am authenticated as "test-client@company.com"

  Scenario: Retrieve an existing order
    Given an order "CMD-001" exists with status "pending"
    When I GET /api/orders/CMD-001
    Then the HTTP status is 200
    And the response contains fields "id", "status", "created_at", "items"
    And the field "status" equals "pending"

  Scenario: Order not found
    When I GET /api/orders/UNKNOWN
    Then the HTTP status is 404
    And the field "error_code" equals "order_not_found"
```

#### Business feature — multi-service flow (co-written with PO)

```gherkin
# features/business/order_flow.feature — Product Owner + dev team

Feature: Complete order flow

  Scenario: A customer places and confirms an order
    Given the catalogue contains product "PROD-001" in stock
    And I am logged in as customer "alice@company.com"
    When I create an order with product "PROD-001"
    And I confirm payment with the test card
    Then the order appears in my history with status "confirmed"
    And I receive a confirmation email
    And the stock of "PROD-001" is decremented by 1

  Scenario: Order attempt on out-of-stock product
    Given product "PROD-002" is out of stock
    When I attempt to order product "PROD-002"
    Then the API returns error "stock_insufficient"
    And no order is created
```

#### E2E feature — critical UI journey (Playwright)

```gherkin
# features/e2e/critical_journeys.feature

Feature: Critical user journeys

  Scenario: Login and dashboard access
    Given I am on the login page
    When I enter valid credentials
    Then I am redirected to the dashboard
    And the navigation menu is visible

  Scenario: Order cancellation from the UI
    Given I am logged in and have a "pending" order
    When I click "Cancel order"
    And I confirm in the modal
    Then the order shows status "cancelled" in the interface
```

### 10.5 API steps implementation

```python
# steps/api_steps.py
import requests
from behave import given, when, then

@given('I am authenticated as "{email}"')
def step_auth(context, email):
    r = requests.post(
        f"{context.config.base_url}/auth/login",
        json={"email": email, "password": context.config.test_password}
    )
    context.token   = r.json()["access_token"]
    context.headers = {"Authorization": f"Bearer {context.token}"}

@when('I GET {endpoint}')
def step_get(context, endpoint):
    context.response = requests.get(
        f"{context.config.base_url}{endpoint}",
        headers=context.headers
    )

@then('the HTTP status is {code:d}')
def step_status(context, code):
    assert context.response.status_code == code, (
        f"Expected {code}, got {context.response.status_code}\n"
        f"Body: {context.response.text}"
    )

@then('the response contains fields "{fields}"')
def step_fields(context, fields):
    body = context.response.json()
    for field in [f.strip() for f in fields.split(",")]:
        assert field in body, f"Field '{field}' missing from response"
```

### 10.6 Environment configuration

```python
# utils/config.py
import os
from dataclasses import dataclass

@dataclass
class Config:
    base_url:      str
    keycloak_url:  str
    test_password: str
    headless:      bool = True

def load_config() -> Config:
    return Config(
        base_url      = os.environ["BASE_URL"],
        keycloak_url  = os.environ.get("KEYCLOAK_URL", ""),
        test_password = os.environ["TEST_PASSWORD"],
        headless      = os.environ.get("HEADLESS", "true") == "true",
    )
```

Run against any environment:

```bash
# Ephemeral test env (pipeline)
BASE_URL=https://my-service.test-ephemeral.cluster.com \
TEST_PASSWORD=xxx \
behave features/api/ features/business/ --no-capture

# Staging E2E smoke
BASE_URL=https://my-service.staging.cluster.com \
HEADLESS=true behave features/e2e/

# POC env
BASE_URL=https://my-service.poc-newauth.cluster.com \
behave features/api/
```

### 10.7 Allure report — readable by PO

```bash
pip install allure-behave
behave -f allure_behave.formatter:AllureFormatter \
       -o reports/allure-results features/

allure generate reports/allure-results -o reports/allure-report --clean
```

Jenkins publishes the HTML report after every run — the PO accesses an interface showing scenarios in natural language, pass/fail status, and error details without reading any code.

### 10.8 Full Jenkinsfile

```groovy
pipeline {
    agent any
    environment {
        SERVICE      = "my-service"
        BASE_VERSION = sh(script: "cat version.txt", returnStdout: true).trim()
        GIT_SHA      = sh(script: "git rev-parse --short HEAD", returnStdout: true).trim()
        GIT_TAG      = sh(script: "git describe --tags --exact-match 2>/dev/null || echo ''",
                          returnStdout: true).trim()
        SNAPSHOT_TAG = "${BASE_VERSION}-SNAPSHOT-${GIT_SHA}"
        RELEASE_TAG  = "${BASE_VERSION}"
    }

    stages {

        stage('Validate Version') {
            steps {
                sh 'python scripts/validate_version.py'
            }
        }

        stage('Unit Tests') {
            steps {
                sh 'pytest tests/unit/ --cov=src --cov-report=xml'
            }
        }

        stage('Sonar Quality Gate') {
            steps {
                withSonarQubeEnv('sonarqube') { sh 'sonar-scanner' }
                timeout(time: 5, unit: 'MINUTES') {
                    waitForQualityGate abortPipeline: true
                }
            }
        }

        stage('Build & Publish Snapshot') {
            steps {
                sh """
                    docker build -t ${SERVICE}:${SNAPSHOT_TAG} .
                    docker push registry.artifactory.company.com/docker-local/${SERVICE}:${SNAPSHOT_TAG}
                    helm package ./helm --version ${SNAPSHOT_TAG}
                    helm push ${SERVICE}-${SNAPSHOT_TAG}.tgz \
                        oci://registry.artifactory.company.com/helm-local
                """
            }
        }

        stage('Gherkin API Tests') {
            steps {
                sh "python platform/deploy.py test-ephemeral ${SERVICE} --tag ${SNAPSHOT_TAG}"
                dir('integration-tests') {
                    sh """
                        BASE_URL=https://${SERVICE}.test-ephemeral.cluster.com \
                        TEST_PASSWORD=${TEST_PASSWORD} \
                        behave features/api/ features/business/ \
                               -f allure_behave.formatter:AllureFormatter \
                               -o reports/allure-results --no-capture
                    """
                }
            }
            post {
                always {
                    sh "python platform/teardown.py test-ephemeral ${SERVICE}"
                    allure includeProperties: false, jdk: '',
                           results: [[path: 'integration-tests/reports/allure-results']]
                }
                failure { error "API tests failed — release promotion cancelled" }
            }
        }

        stage('Promote to Release') {
            when {
                allOf {
                    branch 'main'
                    not { equals expected: '', actual: "${GIT_TAG}" }
                }
            }
            steps {
                sh """
                    python platform/promote.py \
                        --service ${SERVICE} \
                        --from-tag ${SNAPSHOT_TAG} \
                        --to-tag ${RELEASE_TAG}
                """
            }
        }

        stage('Deploy Staging') {
            when { branch 'main' }
            steps {
                sh "python platform/deploy.py staging ${SERVICE} --tag ${RELEASE_TAG}"
            }
        }

        stage('E2E Smoke Playwright') {
            when { branch 'main' }
            steps {
                dir('integration-tests') {
                    sh """
                        BASE_URL=https://${SERVICE}.staging.cluster.com \
                        HEADLESS=true \
                        behave features/e2e/ \
                               -f allure_behave.formatter:AllureFormatter \
                               -o reports/allure-e2e --no-capture || true
                    """
                }
            }
            post {
                always {
                    allure includeProperties: false, jdk: '',
                           results: [[path: 'integration-tests/reports/allure-e2e']]
                }
                failure {
                    slackSend channel: '#quality',
                              message: "⚠️ E2E smoke failed on staging — ${SERVICE} ${RELEASE_TAG}"
                }
            }
        }
    }
}
```

### 10.9 Testing on a POC environment

API tests run directly in the POC environment — no separate ephemeral env needed. The POC is already the isolation boundary.

```bash
BASE_URL=https://central-service-a.poc-newauth.cluster.com \
behave features/api/ features/business/
```

Results are visible in the POC dashboard (section 9).

---

## 11. Flow Summary

### Standard flow — feature → develop → staging → prod

```
commit on develop
    ├── Validate version.txt             [blocking]
    ├── Unit tests + Sonar               [blocking]
    ├── Build snapshot → Artifactory
    ├── Integration tests                [manual on dev, blocking before promotion]
    ├── Promote to release → Artifactory
    ├── Deploy dev
    ├── Deploy staging
    └── E2E smoke Playwright             [non-blocking, alert on failure]
```

### POC flow

```
branch poc/mypoc
    ├── Validate version.txt             [blocking]
    ├── Unit tests + Sonar               [blocking]
    ├── Build poc-mypoc → docker-poc
    ├── Deploy poc-mypoc-*               [namespace guardrails]
    ├── API tests on POC env             [manual or automatic]
    ├── Dashboard: TTL + drift + PR links
    └── On expiry → automatic teardown
        or on success → PR poc/mypoc → develop
```

### Drift detection flow — polling every 15 min

```
drift_checker.py
    ├── Compare version.yaml vs helm list (all envs including POC)
    ├── On drift → Slack notification + log
    ├── Auto-remediation on dev only
    └── check_poc_expiry() → alert 6h before / teardown on expiry
```

### Cleanup flow — weekly

```
cleanup.py (every Sunday at 2am)
    ├── Get all deployed versions (guardrail)
    ├── Delete snapshots older than 7 days (if not deployed)
    ├── Delete RC artifacts older than 30 days
    ├── Delete release artifacts outside retention window (if not deployed)
    └── Delete POC artifacts (managed by TTL teardown)
```

---

## 12. Known Limitations (by design)

| Feature | Status | Workaround |
|---|---|---|
| Continuous reconciliation | ✗ absent | 15-min polling |
| Sync visualization UI | ✗ absent | POC dashboard + Jenkins logs |
| Declarative rollback | ✗ absent | `helm rollback` in a script |
| Raw resource drift (ConfigMap, Secret) | ✗ absent | `kubectl diff` to add if needed |
| Performance testing | ✗ absent | k6 or Locust if required |

These limitations are acceptable for the current scope. ArgoCD can be introduced at any time without changing the platform repo structure.
