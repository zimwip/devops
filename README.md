# AP3 Platform Toolkit

This repository is the **AP3 bootstrap toolkit** — its sole purpose is to create and configure a new AP3 platform instance.

## Repository Layout

```
bootstrap/       Entry point for creating a new platform instance
lib-extras/      Library repos that bootstrap creates and pushes to git hosting
  jenkins-shared-lib/    Groovy Jenkins pipeline shared library
  lib-platform-bom/      Maven BOM for shared dependency versions
platform/        Platform source template (copied to a new repo by bootstrap)
testenv/         Local test environment (Gitea, Jenkins, SonarQube, k3d)
```

Each subdirectory of `lib-extras/` is treated as an independent library repo: bootstrap creates the repo in your git hosting, pushes the contents, and records it in the platform instance under `platform.yaml:libraries` and `libs/<name>.yaml`. To add a new shared library, add a directory here and re-run bootstrap (or use `platform lib register` to reference an existing URL).

## Quick Start

### 1. Create a test environment (optional)

```bash
cd testenv && ./create.sh
```

This starts Gitea, Jenkins, SonarQube, and a local k3d Kubernetes cluster.

### 2. Bootstrap a platform instance

```bash
# Interactive
./bootstrap/bootstrap.sh

# Non-interactive against testenv
set -a && source testenv/.env && set +a
./bootstrap/bootstrap.sh --config testenv/bootstrap-config.yaml
```

Bootstrap will:
- Copy `platform/` to a new directory (default: `../platform`)
- Configure it with your GitHub/Jenkins/cluster settings
- Create the platform and all library repos in git hosting (from `lib-extras/`)
- Record each library in `platform.yaml:libraries` and `libs/<name>.yaml`
- Create the standard environments (dev, val, prod)

### 3. Work from the platform instance

```bash
cd ../platform       # or whatever you set as platform_target_dir
./platform.sh env list
./platform.sh svc create <name> <owner> --template springboot|react|python-api
make dev             # Start API + dashboard
```

## Remove a Platform

```bash
./bootstrap/delete.sh
```

Removes the platform from GitHub, Jenkins, and SonarQube, then deletes the local directory.
Use `--keep-*` flags to skip individual steps.

## Documentation

See `platform/docs/` for full operations guides, architecture overview, and service hook reference.
