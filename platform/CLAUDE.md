# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Is

**AP3 Platform** — a DevOps automation platform that serves as the single source of truth for environment state, service templates, and CI/CD pipelines. It provides:
- A CLI and web dashboard for managing services and environments
- Shared Jenkins pipeline library for all platform-managed services
- Service scaffold templates (Spring Boot, React, Python/FastAPI)
- Helm chart delivery to OpenShift and AWS EKS clusters

## Common Commands

```bash
# Install all dependencies (Python + Node)
make install

# Start full dev environment (API on :5173, UI on :5174)
make dev

# Start components separately
make dev-api       # FastAPI backend only (uvicorn, hot-reload)
make dev-ui        # React/Vite frontend only

# Run backend tests
make test
# Or directly (supports pytest flags):
pytest dashboard/backend/tests/ -v
pytest dashboard/backend/tests/test_api.py::test_env_list -v

# Lint everything
make lint
# Python only: ruff check scripts/ dashboard/backend/
# JS only: cd dashboard/frontend && npx eslint src/

# Platform CLI (use ./platform.sh for all CLI ops)
./platform.sh env list
./platform.sh svc list
./platform.sh svc create <name> <owner> --template springboot|react|python-api
./platform.sh deploy <service> <version> <env>
./platform.sh poc create <name> [--base staging]
./platform.sh poc destroy poc-<name>-<date>
```

## Architecture

### Environment State Model

The `envs/` directory is the authoritative state store — each `envs/{env}/versions.yaml` tracks what version of each service is deployed to that environment. Git history of these files is the audit log. Never edit versions.yaml manually; go through the deployer or CLI.

Three permanent environments: `dev`, `val`, `prod`. Ephemeral `poc-*` environments have TTLs and are cloned from a base environment's service versions.

### CLI Architecture (`scripts/`)

`platform_cli.py` is the entry point; it delegates to domain modules:
- `config.py` — Loads `platform.yaml` and resolves cluster/registry/credential config
- `env_manager.py` — Reads/writes `envs/*/versions.yaml`; handles POC lifecycle
- `service_creator.py` — Three modes: **template** (new repo from scaffold), **fork** (clone existing), **external** (register user-owned repo)
- `deployer.py` — Two paths: Jenkins (if `JENKINS_TOKEN` set) or direct Helm
- `cluster_manager.py` — Cluster configuration CRUD
- `history.py` — Parses git commit history of `envs/` for audit timeline
- `identity.py` — Resolves current user via git config, OS, or GitHub API

### Dashboard Architecture (`dashboard/`)

- **Backend** (`dashboard/backend/app.py`) — FastAPI server; wraps the same domain modules used by the CLI. Routes: `/api/envs`, `/api/services`, `/api/deploy`, `/api/templates`. Also serves the built React frontend at `/`.
- **Frontend** (`dashboard/frontend/src/App.jsx`) — Single-file React SPA (Vite); fetches from `/api/*`. In dev, Vite proxies API calls to the uvicorn backend.
- **Tests** (`dashboard/backend/tests/`) — ~50 pytest tests that work against real `envs/` YAML files (no mocking of the file system).

### Jenkins Shared Library (`lib-extras/jenkins-shared-lib/vars/` in the toolkit root)

`buildService.groovy` is the unified pipeline for all services. It branches on language (Maven vs npm vs Python) and on Git branch (`develop` → auto-deploy to dev, `release/*` → staging, `main` → manual approval → prod). Services customize behavior via `.ap3/hooks.yaml` in their own repos.

### Service Hooks (`.ap3/hooks.yaml` in service repos)

Services can inject shell scripts (`pre_build.sh`, `post_deploy.sh`, etc.) and override Helm values, health check paths, Slack channels, and quality gate settings without modifying platform code. Hook scripts receive env vars: `AP3_SERVICE`, `AP3_VERSION`, `AP3_ENV`, `AP3_NAMESPACE`, `AP3_CLUSTER`, `AP3_PLATFORM`, `AP3_REGISTRY`, `AP3_BRANCH`.

### Deployment Flow

1. Developer pushes to service repo → Jenkins multibranch pipeline picks up via `buildService.groovy`
2. Pipeline calls `deployService.groovy` → which calls `platform.sh deploy` on the platform repo
3. `deployer.py` updates `envs/{env}/versions.yaml` and commits (creating the audit entry)
4. Helm chart is applied to the target cluster (OpenShift or AWS EKS, selected by cluster config)

### Configuration (`platform.yaml`)

Top-level config for GitHub org, Jenkins URL, container registries, and cluster definitions. Clusters map a logical name to a platform type (`openshift` or `aws`), API URL, kubeconfig context, and Helm values suffix. Credentials are always env vars (`GITHUB_TOKEN`, `JENKINS_TOKEN`, `JENKINS_USER`).

## Commit Convention

Conventional Commits enforced by pre-commit hook:
```
feat(env-manager): add TTL extension command
fix(deployer): handle missing namespace gracefully
docs: update POC lifecycle guide
chore: bump fastapi version
```
Types: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `perf`
