# Contributing to the Platform

## Local setup

```bash
git clone git@github.com:my-org/platform.git
cd platform
./bootstrap.sh       # installs all dependencies
make dev             # starts API (:5173) + UI (:5174)
```

## Commit convention

All commits must follow **Conventional Commits**:

```
feat(env-manager): add TTL extension command
fix(deployer): handle missing namespace gracefully
docs: update POC lifecycle guide
chore: bump fastapi to 0.111
```

Types: `feat` `fix` `docs` `chore` `refactor` `test` `perf`

A `commitlint` hook enforces this on push.

## Adding a new service template

1. Create `templates/<your-template>/`
2. Add at minimum: `README.md`, `Jenkinsfile`, `service-manifest.yaml`, `Dockerfile`
3. Use `{{SERVICE_NAME}}`, `{{OWNER}}`, `{{DESCRIPTION}}`, `{{DATE}}` as placeholders
4. Test locally: `python scripts/platform.py service create --name test-svc --template <your-template> --owner me --no-github --no-jenkins`
5. Open a PR against `develop`

## Modifying the CLI

All CLI modules live in `scripts/`. The entry point is `platform.py`.
Tests live alongside the backend in `dashboard/backend/tests/`.

```bash
make test     # run the full test suite
```

## Modifying the dashboard

```bash
make dev-ui   # hot-reload React dev server at :5174
```

The React app is a single file at `dashboard/frontend/src/App.jsx`.
The FastAPI backend is `dashboard/backend/app.py`.

Both share the same domain model defined in `scripts/config.py`.

## Releasing a new platform version

The platform repo itself follows the same semver convention:

```bash
git checkout -b release/1.1.0
# bump version in platform.yaml
git commit -m "chore: release platform 1.1.0"
# PR → main → Jenkins tags and publishes release notes
```
