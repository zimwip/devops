# AP3 Platform

Central repository for the AP3 platform — the single source of truth for environment state, service templates, CI/CD pipelines, and the operations dashboard.

> **Full documentation**: [`docs/OPERATIONS.md`](docs/OPERATIONS.md)  
> **Architecture guide**: [`docs/devops-guide.html`](docs/devops-guide.html) (open in browser)  
> **POC environments**: [`docs/poc-environments.md`](docs/poc-environments.md)

---

## Quick start

### Linux / macOS

```bash
make install       # Install Python + Node dependencies
make dev           # Start API + dashboard
# API  → http://localhost:5173  (Swagger UI at /docs)
# UI   → http://localhost:5174
```

### Python directly (all platforms)

```bash
pip install -r scripts/requirements.txt
python scripts/platform_cli.py --help
```

---

## Key commands

| Action | Linux/macOS |
|---|---|
| List environments | `./platform.sh env list` |
| List services | `./platform.sh svc list` |
| Create POC env | `./platform.sh poc create <name>` |
| Create service | `./platform.sh svc create <name> <owner> --template springboot` |
| Deploy | `./platform.sh deploy <svc> <version> <env>` |
| Run tests | `make test` |
| Audit history | `./platform.sh history` |

---

## Repository layout

```
platform/
├── docs/
│   ├── OPERATIONS.md            ← Full CLI + API operations guide
│   ├── devops-guide.html        ← Architecture guide (self-contained HTML)
│   └── poc-environments.md     ← POC lifecycle guide
├── envs/                        ← Environment state (Git = audit log)
│   ├── dev/versions.yaml
│   ├── val/versions.yaml
│   └── prod/versions.yaml
├── services/                    ← Service catalog entries
├── templates/                   ← Service scaffold templates
│   ├── springboot/              ← Java 21 + Spring Boot 3.4
│   ├── react/                   ← React 18 + Vite + nginx
│   └── python-api/              ← Python 3.12 + FastAPI
├── scripts/                     ← CLI + platform logic (Python)
│   ├── platform_cli.py          ← Main entry point
│   ├── service_creator.py       ← Service bootstrap
│   ├── env_manager.py           ← Environment lifecycle
│   ├── deployer.py              ← Helm / Jenkins deployments
│   └── config.py                ← Platform configuration
├── libs/                        ← Per-library reference files (name, repo_url, source_dir)
├── dashboard/
│   ├── backend/app.py           ← FastAPI server (REST API + Swagger UI)
│   └── frontend/src/App.jsx     ← React dashboard SPA
├── platform.yaml                ← Platform configuration (set by bootstrap)
├── Makefile                     ← Developer shortcuts
├── platform.sh                  ← CLI launcher (Linux/macOS)
├── platform.bat                 ← CLI launcher (Windows CMD)
└── platform.ps1                 ← CLI launcher (Windows PowerShell)
```

---

## Environment types

| Type | Naming | Lifetime | Deploy trigger |
|---|---|---|---|
| `dev` | fixed | permanent | auto on `develop` branch |
| `val` | fixed | permanent | auto on `release/*` branch |
| `prod` | fixed | permanent | manual approval on `main` |
| `poc-*` | `poc-{name}-{date}` | ephemeral (TTL) | on demand |

---

## Prerequisites

| Tool | Required | Notes |
|---|---|---|
| Python ≥ 3.11 | Yes | `python3` |
| Git | Yes | Audit log backbone |
| Node.js ≥ 20 | Optional | Dashboard React UI only |
| Helm ≥ 3.14 | Optional | Direct Kubernetes deployments |
| `kubectl` | Optional | POC namespace deletion |
