# AP3 Service Hooks

AP3 uses a **well-known directory** convention to let individual services customise their build, deploy, and lifecycle behaviour without modifying the shared Jenkins library or platform tooling.

Any service — whether AP3-hosted or externally hosted — can include an `.ap3/` directory at its root.

---

## Directory structure

```
my-service/
└── .ap3/
    ├── hooks.yaml          ← main hook configuration
    ├── pre-build.sh        ← runs before the Maven/npm build
    ├── post-build.sh       ← runs after a successful build
    ├── pre-deploy.sh       ← runs before helm upgrade on each env
    ├── post-deploy.sh      ← runs after helm upgrade succeeds
    └── validate.sh         ← runs after deploy, before smoke tests
```

All scripts are optional. AP3 only executes a script if the file exists and is executable.

---

## `hooks.yaml` reference

```yaml
# .ap3/hooks.yaml
# All fields are optional. Omit any section you don't need.

service:
  ap3_hosted: true             # true = AP3 created and owns the GitHub repo
                               # false = external project referenced in AP3

build:
  skip_quality_gate: false     # skip SonarQube (e.g. for POC branches)
  extra_maven_args: ""         # appended to every mvn command
  extra_npm_args: ""           # appended to every npm command
  docker_build_args: {}        # key/value pairs passed as --build-arg

deploy:
  helm_extra_values: {}        # merged into helm --set arguments on every deploy
  rollback_on_failure: true    # auto rollback if post-deploy health check fails
  health_check_path: /actuator/health   # path polled after deploy
  health_check_timeout_s: 120  # seconds to wait for healthy status

hooks:
  pre_build:   .ap3/pre-build.sh     # path relative to repo root
  post_build:  .ap3/post-build.sh
  pre_deploy:  .ap3/pre-deploy.sh    # receives ENV, VERSION as env vars
  post_deploy: .ap3/post-deploy.sh   # receives ENV, VERSION, NAMESPACE
  validate:    .ap3/validate.sh      # receives ENV, NAMESPACE, HEALTH_URL

notifications:
  slack_channel: "#my-service-deploys"   # overrides platform default channel
  notify_on: [deploy_start, deploy_ok, deploy_fail, rollback]
```

---

## Hook script environment variables

All hook scripts receive these environment variables from Jenkins:

| Variable | Example | Description |
|---|---|---|
| `AP3_SERVICE` | `my-service` | Service name |
| `AP3_VERSION` | `2.3.0` | Version being built/deployed |
| `AP3_ENV` | `prod` | Target environment |
| `AP3_NAMESPACE` | `platform-prod` | Kubernetes namespace |
| `AP3_CLUSTER` | `openshift-prod` | Cluster name |
| `AP3_PLATFORM` | `openshift` | `openshift` or `aws` |
| `AP3_REGISTRY` | `registry.internal` | Container registry |
| `AP3_BRANCH` | `release/2.3.0` | Git branch that triggered the build |

---

## Example hooks

### `pre-deploy.sh` — run database migrations before deploy

```bash
#!/usr/bin/env bash
set -euo pipefail
echo "Running DB migrations for ${AP3_SERVICE}:${AP3_VERSION} on ${AP3_ENV}"
kubectl exec -n "${AP3_NAMESPACE}" deploy/my-service -- \
    java -jar /app/app.jar --migrate-only
```

### `post-deploy.sh` — invalidate CDN cache after frontend deploy

```bash
#!/usr/bin/env bash
set -euo pipefail
if [[ "$AP3_ENV" == "prod" ]]; then
    aws cloudfront create-invalidation \
        --distribution-id "${CDN_DISTRIBUTION_ID}" \
        --paths "/*"
fi
```

### `validate.sh` — custom smoke test

```bash
#!/usr/bin/env bash
set -euo pipefail
RESPONSE=$(curl -sf "${AP3_HEALTH_URL}/readyz" || true)
if [[ "$RESPONSE" != *"UP"* ]]; then
    echo "Service not ready — validate failed"
    exit 1
fi
echo "Validation passed"
```

---

## `hooks.yaml` discovery

When Jenkins runs a build for a service, it:

1. Checks for `.ap3/hooks.yaml` in the repo root
2. Merges the hook config with platform defaults (service config wins)
3. Executes scripts in order: `pre_build` → build → `post_build` → `pre_deploy` → helm → `post_deploy` → `validate`
4. If any hook exits non-zero, the stage fails and (if `rollback_on_failure: true`) rolls back

---

## External services (`ap3_hosted: false`)

For services not created by AP3, the `.ap3/hooks.yaml` is the **only** configuration needed to integrate with the platform. The service's existing repository is referenced by URL and AP3 creates only a Jenkins pipeline that points to it. The scaffold, branch protection, and template steps are skipped.

```yaml
# .ap3/hooks.yaml in an external service
service:
  ap3_hosted: false

deploy:
  helm_chart_path: ./deploy/helm    # non-standard Helm chart location
  health_check_path: /health
```

---

## Forked services

When a service is created by forking an existing AP3-hosted service, the fork inherits the source's `.ap3/hooks.yaml`. Update it after forking to reflect the new service's specifics (Slack channel, migrations, etc.).
