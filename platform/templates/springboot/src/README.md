# {{SERVICE_NAME}}

{{DESCRIPTION}}

Owner: **{{OWNER}}** — Created: {{DATE}}

## Run locally

```bash
mvn spring-boot:run
# → http://localhost:8080
# → http://localhost:8080/actuator/info   (version + git info)
# → http://localhost:8080/actuator/health
```

## Branch workflow

| Branch | Triggers |
|---|---|
| `feature/*` | Build + unit tests |
| `develop` | Build + tests + deploy to DEV |
| `release/x.y.z` | Build + deploy to STAGING |
| `main` | Build + deploy to PROD (manual approval) |

## Release a new version

```bash
# On release branch: bump pom.xml version (removes -SNAPSHOT)
mvn versions:set -DnewVersion=1.0.0 -DgenerateBackupPoms=false
git add pom.xml && git commit -m "chore: release 1.0.0"
# Merge to main → Jenkins tags and generates release notes automatically
```
