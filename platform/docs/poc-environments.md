# POC Environments — Lifecycle Guide

Ephemeral environments let any developer spin up an isolated namespace
forked from a base environment, deploy experimental service versions,
then destroy everything cleanly. The entire lifecycle is tracked in Git.

## Lifecycle

```
create ──→ deploy (iterate) ──→ validate ──→ destroy
   ↑                                            ↓
   └──────── extends TTL if needed ─────────────┘
```

## Naming convention

```
poc-{short-slug}-{YYYYMMDD}
│    │            └── creation date (auto)
│    └── your descriptive name
└── constant prefix
```

Examples: `poc-payment-kafka-20260403`, `poc-auth-oidc-20260501`

## Creating a POC

### Via CLI
```bash
python scripts/platform_cli.py env create \
  --name payment-kafka \
  --type poc \
  --base staging \
  --description "Test async payment flow with Kafka" \
  --ttl-days 14   # default; max 365
```

### Via Makefile
```bash
make poc-create NAME=payment-kafka BASE=staging
```

### Via dashboard
Open the dashboard → Environments tab → "+ New POC"

## What gets created

1. A new directory `envs/poc-payment-kafka-20260403/` is committed to Git
2. `versions.yaml` is forked from the base environment's versions
3. A dedicated OpenShift namespace `platform-poc-payment-kafka-20260403` is provisioned
4. A PR is opened (or direct commit) — the Git history is the audit log

## Deploying to a POC

```bash
# Deploy an experimental version to your POC
python scripts/platform_cli.py deploy \
  --env poc-payment-kafka-20260403 \
  --service service-payments \
  --version 0.1.0-SNAPSHOT-exp42

# Or via Helm directly (POCs don't require Jenkins approval)
helm upgrade --install service-payments ./service-payments/helm \
  --namespace platform-poc-payment-kafka-20260403 \
  --set image.tag=0.1.0-SNAPSHOT-exp42 \
  --values service-payments/helm/values-dev.yaml
```

## Branch convention

POC branches live alongside the POC namespace:

```
poc/payment-kafka        ← feature code for this experiment
poc/payment-kafka-infra  ← any infra config changes (optional)
```

Jenkins builds `poc/*` branches but **skips** the SonarQube quality gate
(declared in `buildService.groovy` with `when { not { branch 'poc/*' } }`).
This allows fast iteration without blocking on coverage metrics.

## versions.yaml anatomy for a POC

```yaml
_meta:
  env_type: "poc"            # ← distinguishes from fixed envs
  base_env: "staging"        # ← forked from here
  owner: "john.doe"
  description: "Test async payment flow"
  expires_at: "2026-04-17T00:00:00Z"    # ← TTL enforced by cleanup job
  branch_convention: "poc/payment-kafka"

services:
  service-auth:
    version: "2.3.0"          # ← inherited from staging, unchanged
  service-payments:
    version: "0.1.0-SNAPSHOT-exp42"   # ← experimental, only here
    experimental: true
```

## Destroying a POC

```bash
# CLI (asks for confirmation)
python scripts/platform_cli.py env destroy --name poc-payment-kafka-20260403

# Force (no prompt, useful in scripts)
python scripts/platform_cli.py env destroy --name poc-payment-kafka-20260403 --force

# Makefile
make poc-destroy NAME=poc-payment-kafka-20260403
```

This will:
1. Delete the OpenShift namespace (and all its resources)
2. Remove `envs/poc-payment-kafka-20260403/` from the repo
3. Commit the deletion to Git

## TTL deadline — soft expiry, no automatic destruction

Expiry is a **soft deadline**. When a POC passes its `expires_at`, warnings appear in `env info` and the dashboard — but **the environment is never destroyed automatically**. You must act explicitly: extend or destroy.

```groovy
// jenkins-shared-lib/vars/cleanupExpiredPocs.groovy
// Triggered by: cron('H 2 * * *')  — runs at 2am every night
def call() {
    sh """
        python3 /opt/platform/scripts/platform_cli.py env list --json | \
        python3 -c "
import json, sys
from datetime import datetime, timezone
envs = json.load(sys.stdin)
now  = datetime.now(timezone.utc)
for env in envs:
    expires = env.get('expires_at')
    if env.get('type') == 'poc' and expires:
        exp_dt = datetime.fromisoformat(expires)
        if now > exp_dt:
            print(env['name'])
" | xargs -I{} python3 /opt/platform/scripts/platform_cli.py env destroy --name {} --force
    """
}
```

## Promoting from POC to staging

Once the experiment is validated, promote the version through the normal flow:

```bash
# 1. Merge poc/payment-kafka → develop
git checkout develop && git merge poc/payment-kafka

# 2. Jenkins builds develop → deploys to DEV automatically

# 3. Create release branch to promote to staging
git checkout -b release/1.10.0
mvn versions:set -DnewVersion=1.10.0

# 4. Jenkins deploys release/1.10.0 → staging automatically
```
