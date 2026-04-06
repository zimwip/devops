# Platform — Operations Guide

Complete reference for installing, running and operating the AP3 platform CLI and dashboard.

---

## Table of contents

1. [Prerequisites](#prerequisites)
2. [Installation](#installation)
   - [Linux / macOS](#linux--macos)
   - [Windows](#windows)
3. [Configuration](#configuration)
4. [CLI reference](#cli-reference)
   - [Service commands](#service-commands)
   - [Environment commands](#environment-commands)
   - [Deploy command](#deploy-command)
   - [Release notes command](#release-notes-command)
5. [Dashboard (web UI + API)](#dashboard-web-ui--api)
   - [Start the dashboard](#start-the-dashboard)
   - [API reference](#api-reference)
6. [POC environments](#poc-environments)
7. [Launcher quick reference](#launcher-quick-reference)
   - [Linux / macOS — Makefile](#linux--macos--makefile)
   - [Windows — platform.bat](#windows--platformbat)
   - [Windows — platform.ps1](#windows--platformps1)
8. [Environment variables](#environment-variables)
9. [Troubleshooting](#troubleshooting)

---

## Prerequisites

| Tool | Version | Required for | Download |
|---|---|---|---|
| Python | ≥ 3.11 | CLI + API backend | https://python.org |
| Git | any | Service creation, env tracking | https://git-scm.com |
| Node.js | ≥ 20 | React dashboard UI | https://nodejs.org |
| Helm | ≥ 3.14 | Kubernetes deployments | https://helm.sh |
| `oc` or `kubectl` | any | POC namespace management | OpenShift / Kubernetes |

Node.js, Helm and `oc`/`kubectl` are **optional** for local development — the CLI degrades gracefully when they are absent and prints a clear warning.

---

## Bootstrap wizard

The bootstrap script runs an interactive wizard that guides you through creating your initial environments. No pre-populated demo data is included by default.

```bash
# Standard bootstrap — wizard creates prod, val, dev and prompts for cluster details
./bootstrap.sh

# With demo data — seeds realistic example services so you can explore history/dashboard
./bootstrap.sh --demo

# Non-interactive — uses all defaults, no prompts (useful in CI)
./bootstrap.sh --yes
./bootstrap.sh --yes --demo

# Windows CMD
bootstrap.bat
bootstrap.bat --demo

# Windows PowerShell
.\bootstrap.ps1
.\bootstrap.ps1 -demo
```

The wizard creates **prod**, **val** (validation/UAT), and **dev** by default — not `staging`. You can rename any environment or add more during the wizard. All environments use `commit: wizard` (not `bootstrap`) so they appear correctly in the audit history.

The `--demo` flag seeds each environment with example service versions (spe, service-auth, service-orders, lib-platform) at realistic version numbers and timestamps, so the dashboard and `history` command show meaningful data immediately.

## Installation

### Linux / macOS

```bash
# 1. Clone the platform repo
git clone git@github.com:my-org/platform.git
cd platform

# 2. Run the bootstrap wizard
chmod +x bootstrap.sh && ./bootstrap.sh

# 3. Verify
python scripts/platform_cli.py env list
python scripts/platform_cli.py history
```

### Windows

**Option A — CMD (platform.bat)**

```cmd
REM 1. Clone
git clone git@github.com:my-org/platform.git
cd platform

REM 2. Bootstrap
bootstrap.bat

REM 3. Verify
platform.bat help
platform.bat env list
```

**Option B — PowerShell (platform.ps1)**

```powershell
# Allow local scripts (one-time, current user only)
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned

# 1. Clone
git clone git@github.com:my-org/platform.git
cd platform

# 2. Bootstrap
.\bootstrap.ps1

# 3. Verify
.\platform.ps1 help
.\platform.ps1 env list
```

**Option C — Python directly (all platforms)**

```bash
# Works identically on Linux, macOS and Windows
python scripts/platform_cli.py --help
python scripts/platform_cli.py env list
```

> **Note — Windows PATH**: make sure `python`, `git` and (optionally) `node` are on your `%PATH%`. The Python installer offers this as a checkbox ("Add Python to PATH") during setup.

---

## Configuration

Edit `platform.yaml` at the repo root:

```yaml
# platform.yaml
github_org: "my-org"              # GitHub organisation name
github_token_env: "GITHUB_TOKEN"  # env var holding the token

jenkins_url: "https://jenkins.internal"
jenkins_user_env: "JENKINS_USER"
jenkins_token_env: "JENKINS_TOKEN"

registry: "registry.internal"     # Docker image registry

default_cluster_dev:  "openshift-dev"
default_cluster_prod: "openshift-prod"
```

Sensitive values (tokens, passwords) are **never** stored in `platform.yaml`. They are read from environment variables at runtime. See [Environment variables](#environment-variables).

---


## Confirmation step

Every state-mutating CLI command (`service create`, `env create`, `env destroy`, `deploy`) displays a **confirmation disclaimer** before executing. The disclaimer shows:

- **Who** will perform the actions (GitHub identity from the token, Jenkins user, local git config)
- **What** will happen (list of concrete actions)
- **Warnings** for any missing or invalid tokens

```
  ┌─ Confirmation required ────────────────────────────────────┐
  │
  │  The following changes will be performed on behalf of:
  │
  │    GitHub   : Jane Doe (@janedoe)
  │               jane@example.com
  │    Jenkins  : jane.doe  (https://jenkins.internal)
  │    Git      : Jane Doe <jane@example.com>
  │
  │  Actions:
  │    · Create GitHub repo my-org/my-service
  │    · Set branch protection on main + develop
  │    · Register Jenkins pipeline 'my-service'
  │    · Register service in dev versions.yaml
  │
  └────────────────────────────────────────────────────────────┘

  Proceed? [y/N]
```

### Skip confirmation with `--force`

The `--force` flag prints the disclaimer (for audit purposes) but skips the `Proceed? [y/N]` prompt. Use it in CI scripts, automation, or when you want to confirm visually without typing.

```bash
# Linux / macOS
python scripts/platform_cli.py service create --name my-svc --template springboot --owner team-x --force
python scripts/platform_cli.py env create --name my-poc --base staging --force
python scripts/platform_cli.py deploy --service service-auth --version 2.3.0 --env dev --force
python scripts/platform_cli.py env destroy --name poc-my-poc-20260403 --force

# Makefile
make svc-create NAME=my-svc TEMPLATE=springboot OWNER=team-x FORCE=1
make poc-create NAME=my-poc BASE=staging FORCE=1
make deploy SVC=service-auth VERSION=2.3.0 ENV=dev FORCE=1

# Windows CMD
platform.bat deploy service-auth 2.3.0 dev --force

# Windows PowerShell
.\platform.ps1 deploy service-auth 2.3.0 dev --force
```

### Dashboard confirmation

The web dashboard shows its own confirmation modal before any action. It calls `/api/identity` to resolve the acting identity in real time, then displays the same actor information and action list. No `--force` is needed from the dashboard — confirmation happens in the UI before the API call is made.

---
## CLI reference

All commands share these global flags:

| Flag | Description |
|---|---|
| `--dry-run` | Print what would happen without executing |
| `--json` | Output results as JSON (useful for scripting) |
| `--config PATH` | Path to a custom `platform.yaml` |

### Service commands

#### `service create` — Bootstrap a new AP3 service

AP3 supports three modes for creating a service. Choose based on where the code lives.

---

**Mode 1 — AP3-hosted from template** *(default)*

Creates a new GitHub repo in the AP3 org, scaffolded from a built-in template.

```bash
# Linux / macOS
python scripts/platform_cli.py service create \
  --name my-service \
  --template springboot \
  --owner team-backend

# With description
python scripts/platform_cli.py service create \
  --name my-service \
  --template python-api \
  --owner team-data \
  --description "ML inference API"

# Windows CMD
platform.bat svc create my-service springboot team-backend
```

**Available templates:**

| Template | Language / Framework | Use case |
|---|---|---|
| `springboot` | Java 21, Spring Boot 3.4 | Backend REST API, worker |
| `react` | TypeScript, React 18, Vite | Frontend single-page app |
| `python-api` | Python 3.12, FastAPI | Lightweight API, ML service |

---

**Mode 2 — AP3-hosted fork**

Clone an existing AP3-hosted service into a new independent repo. The fork inherits the Jenkinsfile, Helm chart, and `.ap3/hooks.yaml` of the source.

```bash
python scripts/platform_cli.py service create \
  --name payment-v2 \
  --fork-from service-payments \
  --owner team-payments
```

After forking, update `.ap3/hooks.yaml` in the new repo to reflect its specific requirements (Slack channel, migration hooks, etc.).

---

**Mode 3 — External repo**

Reference an existing repository that AP3 did not create. No scaffolding, no GitHub repo creation. AP3 only registers the service in Jenkins and the dev `versions.yaml`.

```bash
python scripts/platform_cli.py service create \
  --name legacy-billing \
  --external-repo https://github.com/my-org/legacy-billing.git \
  --owner team-finance
```

The external repo can add a `.ap3/hooks.yaml` at any time to customise deploy behaviour. See `docs/service-hooks.md`.

---

**All modes — common flags:**

| Flag | Description |
|---|---|
| `--name` | Service name in `kebab-case` (3–50 chars) — required |
| `--owner` | Owning team — required |
| `--description` | Short description |
| `--no-jenkins` | Skip Jenkins pipeline registration |
| `--force` | Skip confirmation prompt |

**What gets created:**

```
my-service/                        ← scaffold from template
├── Jenkinsfile                    ← uses platform shared library
├── service-manifest.yaml          ← declares inter-service dependencies
├── Dockerfile
└── helm/                          ← Helm chart with per-env values

GitHub: my-org/my-service          ← private repo, branch protection on main+develop
Jenkins: my-service (multibranch)  ← triggers on push to any branch
envs/dev/versions.yaml             ← service registered at 0.1.0-SNAPSHOT
```

---

#### `service list` — List all services

```bash
python scripts/platform_cli.py service list
python scripts/platform_cli.py service list --json

# Launchers
make svc-list
platform.bat svc list
.\platform.ps1 svc list
```

**Output:**
```
  Service                         dev             staging         prod            Last deployed
  ──────────────────────────────────────────────────────────────────────────────────────
  lib-platform                    1.4.0-SNAPSHOT  1.3.0           1.3.0           2026-03-28T14:32:00Z
  service-auth                    2.4.1-SNAPSHOT  2.3.0           2.2.1           2026-03-28T14:32:00Z
```

---

#### `service info` — Show service details

```bash
python scripts/platform_cli.py service info --name service-auth
python scripts/platform_cli.py service info --name service-auth --json

# Launchers
platform.bat svc info service-auth
.\platform.ps1 svc info service-auth
```

---

### Environment commands

#### `env list` — List all environments

```bash
python scripts/platform_cli.py env list
python scripts/platform_cli.py env list --json

# Launchers
make env-list
platform.bat env list
.\platform.ps1 env list
```

**Output:**
```
  Environment                           Type        Cluster         Owner / updated by      Expires / updated at
  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
  dev                                   fixed       openshift-dev   jenkins/job/...         permanent
  staging                               fixed       openshift-stg   jenkins/job/...         permanent
  prod                                  fixed       openshift-prod  jenkins/job/...         permanent
  poc-payment-experiment-20260403       poc         openshift-dev   john.doe                2026-04-17T00:00:00+00:00
```

---

#### `env info` — Show environment details

```bash
python scripts/platform_cli.py env info --name prod
platform.bat env info prod
.\platform.ps1 env info prod
```

---

#### `env create` — Create a POC environment

Forks the service versions from a base environment into a new ephemeral namespace. The full name is auto-generated as `poc-{name}-{YYYYMMDD}`.

```bash
# Linux / macOS — OpenShift POC (default, no flags needed)
python scripts/platform_cli.py env create \
  --name payment-experiment \
  --type poc \
  --base staging \
  --description "Test async payment flow with Kafka" \
  --ttl-days 14

# Linux / macOS — AWS / EKS POC
python scripts/platform_cli.py env create \
  --name payment-experiment-aws \
  --base staging \
  --platform aws \
  --cluster eks-dev \
  --namespace my-team-existing-ns \
  --description "Same experiment on EKS"

# Windows CMD
platform.bat env create payment-experiment staging
platform.bat env create payment-aws staging my-ns eks-dev aws

# Windows PowerShell
.\platform.ps1 env create payment-experiment staging
# For cluster/platform flags use the Python CLI directly on Windows:
python scripts\platform_cli.py env create --name my-poc --base staging --platform aws --cluster eks-dev
```

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `--name` | required | Short slug — auto-prefixed with `poc-` |
| `--type` | `poc` | `poc` or `fixed` |
| `--base` | `staging` | Environment to fork versions from |
| `--platform` | from cluster | `openshift` or `aws` — overrides cluster profile |
| `--cluster` | from base env | Cluster name from `platform.yaml` clusters section |
| `--namespace` | auto-generated | Pre-existing namespace (use when you have no NS create rights) |
| `--owner` | git user | POC owner (email) |
| `--description` | `""` | Purpose of this POC |
| `--ttl-days` | `14` | Expiry in days (1–365) |

**What gets created:**

```
envs/poc-payment-experiment-20260403/
└── versions.yaml    ← forked from staging, committed to Git

OpenShift namespace: platform-poc-payment-experiment-20260403
Git commit: "env: create POC environment 'poc-payment-experiment-20260403'"
```

---

#### `env destroy` — Destroy a POC environment

```bash
python scripts/platform_cli.py env destroy --name poc-payment-experiment-20260403
python scripts/platform_cli.py env destroy --name poc-payment-experiment-20260403 --force

# Windows CMD
platform.bat env destroy poc-payment-experiment-20260403
platform.bat poc destroy poc-payment-experiment-20260403

# Windows PowerShell
.\platform.ps1 env destroy poc-payment-experiment-20260403
.\platform.ps1 poc destroy poc-payment-experiment-20260403
```

> **Note**: only POC environments (`env_type: poc`) can be destroyed. Fixed environments (`dev`, `staging`, `prod`) are protected.

---


---

#### `config show` / `config set` — Platform integration settings

```bash
# Show current settings and token status
python scripts/platform_cli.py config show

# Update GitHub org
python scripts/platform_cli.py config set --github-org my-new-org

# Switch to personal user account mode
python scripts/platform_cli.py config set \
  --github-account-type user \
  --github-org myusername

# Point to GitHub Enterprise
python scripts/platform_cli.py config set \
  --github-url https://github.mycompany.com \
  --github-org my-org

# Update Jenkins URL
python scripts/platform_cli.py config set --jenkins-url https://jenkins.mycompany.com
```

These settings are stored in `platform.yaml` (committed, no secrets). Tokens (`GITHUB_TOKEN`, `JENKINS_USER`, `JENKINS_TOKEN`) are managed separately via environment variables or `.env`.

#### `env diff` — Compare versions between environments

```bash
python scripts/platform_cli.py env diff --from staging --to prod
python scripts/platform_cli.py env diff --from staging --to prod --json

# Windows CMD
platform.bat env diff staging prod

# Windows PowerShell
.\platform.ps1 env diff staging prod
```

**Output:**
```
  Diff: staging -> prod

  Service                       staging             prod                Changed
  ────────────────────────────────────────────────────────────────────
  service-auth                  2.3.0               2.2.1               yes
  service-orders                1.9.0               1.9.0
  lib-platform                  1.3.0               1.3.0
```

---

### Deploy command

Triggers a deployment of a specific version to a target environment.

- If `JENKINS_TOKEN` is set → triggers a Jenkins parameterised build
- Otherwise → runs `helm upgrade --install` directly (useful for POC envs)

In both cases, `envs/{env}/versions.yaml` is updated automatically.

```bash
# Linux / macOS — standard deploy (platform derived from env's cluster profile)
python scripts/platform_cli.py deploy \
  --service service-auth \
  --version 2.3.0 \
  --env dev

# Override platform explicitly (e.g. env is on AWS but profile not yet configured)
python scripts/platform_cli.py deploy \
  --service service-auth \
  --version 2.3.0 \
  --env poc-payment-aws-20260403 \
  --platform aws

# With wait for rollout
python scripts/platform_cli.py deploy \
  --service service-auth \
  --version 2.3.0 \
  --env dev \
  --wait

# Windows CMD
platform.bat deploy service-auth 2.3.0 dev

# Windows PowerShell
.\platform.ps1 deploy service-auth 2.3.0 dev

# Makefile
make deploy SVC=service-auth VERSION=2.3.0 ENV=dev
```

---

### Release notes command

```bash
python scripts/platform_cli.py release-notes --service service-auth
python scripts/platform_cli.py release-notes --service service-auth --version 2.2.1
python scripts/platform_cli.py release-notes --service service-auth --json
```

Fetches the GitHub Release for the service. If `GITHUB_TOKEN` is not set, falls back to `git log`.

---

## Dashboard (web UI + API)

### Start the dashboard

**Linux / macOS:**

```bash
# Option A — both servers at once
make dev
# API  → http://localhost:5173  (+ Swagger at /docs)
# UI   → http://localhost:5174

# Option B — API only
make dev-api

# Option C — production mode (API serves built React frontend)
make build      # build the React frontend
make dev-api    # now http://localhost:5173 serves both
```

**Windows CMD:**

```cmd
platform.bat dev          REM opens two CMD windows
platform.bat dev-api      REM API only
platform.bat dev-ui       REM UI only
```

**Windows PowerShell:**

```powershell
.\platform.ps1 dev          # opens two PowerShell windows
.\platform.ps1 dev-api      # API only
.\platform.ps1 dev-ui       # UI only
```

**Python directly (all platforms):**

```bash
# Terminal 1 — API
cd dashboard/backend
uvicorn app:app --reload --port 5173

# Terminal 2 — UI (optional, for development hot-reload)
cd dashboard/frontend
npm run dev
```

### API reference

The Swagger UI at `http://localhost:5173/docs` is the authoritative reference — it shows every endpoint, schema, and lets you try requests interactively.

| Method | Path | Description |
|---|---|---|
| GET | `/api/envs` | List all environments |
| GET | `/api/envs/{name}` | Environment details |
| POST | `/api/envs` | Create POC environment |
| DELETE | `/api/envs/{name}` | Destroy POC environment |
| GET | `/api/envs/{a}/diff/{b}` | Version diff between two envs |
| GET | `/api/services` | List all services (version matrix) |
| GET | `/api/services/{name}` | Single service details |
| POST | `/api/services` | Bootstrap new service |
| POST | `/api/deploy` | Trigger deployment |
| GET | `/api/templates` | List scaffold templates |

**Example — create a POC via the API:**

```bash
curl -X POST http://localhost:5173/api/envs \
  -H "Content-Type: application/json" \
  -d '{
    "name": "payment-experiment",
    "base": "staging",
    "owner": "john.doe",
    "description": "Testing Kafka async payments",
    "ttl_days": 14
  }'
```

**Example — trigger a deployment:**

```bash
curl -X POST http://localhost:5173/api/deploy \
  -H "Content-Type: application/json" \
  -d '{
    "env": "dev",
    "service": "service-auth",
    "version": "2.3.0"
  }'
```

### Run the tests

```bash
# Linux / macOS
make test

# Windows CMD
platform.bat test

# Windows PowerShell
.\platform.ps1 test

# Python directly
python -m pytest dashboard/backend/tests/ -v
```

---

## POC environments

See [`docs/poc-environments.md`](docs/poc-environments.md) for the full lifecycle guide including branch conventions, TTL soft deadlines (no auto-destroy), extend commands, and how to promote a validated experiment to staging.

**Quick workflow:**

```bash
# 1. Create
python scripts/platform_cli.py env create --name my-exp --base staging

# 2. Deploy experimental version
python scripts/platform_cli.py deploy \
  --env poc-my-exp-20260403 \
  --service service-payments \
  --version 0.1.0-SNAPSHOT-exp42

# 3. Iterate, test, validate...

# 4. Destroy when done
python scripts/platform_cli.py env destroy \
  --name poc-my-exp-20260403 --force
```

---

## Launcher quick reference

### Linux / macOS — Makefile

```bash
make help                                           # show all targets
make install                                        # pip + npm deps
make dev                                            # API :5173 + UI :5174
make dev-api                                        # API only
make dev-ui                                         # UI only
make test                                           # pytest
make build                                          # build React frontend
make clean                                          # remove build artefacts
make env-list                                       # list envs
make svc-list                                       # list services
make poc-create NAME=my-poc BASE=staging            # create POC
make poc-destroy NAME=poc-my-poc-20260403           # destroy POC
make svc-create NAME=my-svc TEMPLATE=springboot OWNER=team-x
make deploy SVC=service-auth VERSION=2.3.0 ENV=dev
```

### Windows — platform.bat

```cmd
platform.bat help
platform.bat install
platform.bat dev
platform.bat dev-api
platform.bat dev-ui
platform.bat test
platform.bat build
platform.bat env list
platform.bat env info <n>
platform.bat env create <n> [base]
platform.bat env destroy <n>
platform.bat env diff <from> <to>
platform.bat svc list
platform.bat svc info <n>
platform.bat svc create <n> <template> <owner>
platform.bat deploy <service> <version> <env>
platform.bat poc create <n> [base]
platform.bat poc destroy <n>
```

### Windows — platform.ps1

Same commands as `platform.bat`, just replace `platform.bat` with `.\platform.ps1`:

```powershell
.\platform.ps1 help
.\platform.ps1 install
.\platform.ps1 dev
.\platform.ps1 env create my-poc staging
.\platform.ps1 svc create my-svc springboot team-x
.\platform.ps1 deploy service-auth 2.3.0 dev
.\platform.ps1 poc create my-exp
.\platform.ps1 poc destroy poc-my-exp-20260403
```

---

## Environment variables

Set these in your shell profile, a `.env` file, or Windows System Properties → Environment Variables.

| Variable | Required for | Description |
|---|---|---|
| `GITHUB_TOKEN` | Service creation, release notes | GitHub personal access token with `repo` + `admin:org` scopes |
| `JENKINS_USER` | Deployment via Jenkins | Jenkins username |
| `JENKINS_TOKEN` | Deployment via Jenkins | Jenkins API token |
| `PORT` | Dashboard API | API server port (default: `5173`) |
| `RELOAD` | Dashboard API | Uvicorn hot-reload (`1`=on, `0`=off in prod) |

**.env file (Linux / macOS):**

```bash
cp env.example .env
# edit .env — it is in .gitignore, never committed
```

**Windows CMD (session only):**

```cmd
set GITHUB_TOKEN=ghp_your_token_here
set JENKINS_USER=admin
set JENKINS_TOKEN=your_api_token
```

**Windows PowerShell (session only):**

```powershell
$env:GITHUB_TOKEN = "ghp_your_token_here"
$env:JENKINS_USER = "admin"
$env:JENKINS_TOKEN = "your_api_token"
```

**Windows (permanent — current user):**

```powershell
[System.Environment]::SetEnvironmentVariable("GITHUB_TOKEN", "ghp_...", "User")
```

---

## Troubleshooting

### `python` not found (Linux / macOS)

The scripts use `python3` on Unix and `python` on Windows via the `compat.py` module. If `python3` is not on your PATH:

```bash
# Check
which python3 || which python

# Fix (Ubuntu/Debian)
sudo apt install python3 python-is-python3
```

### `platform_cli.py` shadows stdlib `platform` module

The CLI entry point is named `platform_cli.py` (not `platform.py`) precisely to avoid shadowing Python's built-in `platform` module. If you invoke it via `python -m platform` that will call the stdlib — always use `python scripts/platform_cli.py`.

### PowerShell execution policy error

```
File cannot be loaded because running scripts is disabled on this system.
```

Fix:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

### `uvicorn` not found on Windows

```cmd
pip install uvicorn[standard]
REM or
python -m uvicorn app:app --reload --port 5173
```

### Port 5173 already in use

```bash
# Linux / macOS
PORT=5200 make dev-api

# Windows CMD
set PORT=5200 && platform.bat dev-api

# Windows PowerShell
$env:PORT = "5200"; .\platform.ps1 dev-api
```

### `oc`/`kubectl` not found during POC destroy

The CLI prints a warning and skips namespace deletion. Delete the namespace manually:

```bash
oc delete namespace platform-poc-my-exp-20260403 --ignore-not-found
# or
kubectl delete namespace platform-poc-my-exp-20260403 --ignore-not-found
```

The `envs/` directory entry is still removed from Git so the platform no longer tracks the environment.

### Git commit fails during env create/destroy

This happens when running outside a git repository or when there are no staged changes. The CLI prints a warning and continues — commit the `envs/` changes manually:

```bash
git add envs/
git commit -m "env: create/destroy POC environment"
```
