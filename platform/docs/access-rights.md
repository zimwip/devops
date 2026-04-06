# AP3 — Access Rights & Token Management

This document explains the three modes of operating AP3:

1. **CLI mode** — developers run `platform_cli.py` locally with their own tokens
2. **Dashboard (local)** — a developer runs the dashboard on their machine; tokens come from their `.env`
3. **Dashboard as a platform service** — the dashboard runs inside the cluster, multiple users authenticate and manage their own tokens securely

---

## Mode 1 — CLI / local dashboard

### How it works

Tokens are read from environment variables. The recommended approach is a `.env` file at the platform repo root:

```bash
# .env  (in .gitignore — never committed)
GITHUB_TOKEN=ghp_your_personal_access_token
JENKINS_USER=jane.doe
JENKINS_TOKEN=your_jenkins_api_token
```

Load it before running:
```bash
set -a && source .env && set +a   # Linux / macOS
# or: use direnv (https://direnv.net) which loads .env automatically
```

### Required token scopes

| Token | Required scopes | Used for |
|---|---|---|
| `GITHUB_TOKEN` | `repo` + `admin:org` (org mode) or `repo` (user mode) | Create repos, push, branch protection |
| `JENKINS_TOKEN` | Jenkins API token from Manage Jenkins → Users | Trigger builds, create pipelines |

### GitHub: organisation vs user

`platform.yaml` has `github_account_type: org | user`. With `user`, repos are created under `https://github.com/{username}/{service}` and branch protection is skipped (not available on personal repos without paying). With `org`, the PAT must have `admin:org` scope for branch protection.

### Is `.env` enough?

For **local CLI use**: yes. The file is git-ignored, never committed, and only accessible to whoever has read access to the machine. Rotate tokens via GitHub/Jenkins settings.

For **CI pipelines** (Jenkins running `platform_cli.py`): inject tokens as Jenkins credentials and pass them as environment variables to the build — never store them in the repo.

For **a shared dashboard service**: `.env` is not enough. See Mode 3 below.

---

## Mode 2 — Dashboard as a shared platform service

### The problem

When the dashboard runs as a service inside the cluster (e.g. as an OpenShift Deployment or EKS pod), multiple engineers use it simultaneously. Each engineer has their own GitHub and Jenkins identity. Sharing a single `GITHUB_TOKEN` would mean:

- All actions appear to come from one service account
- Rotating the token breaks everyone simultaneously
- No per-user audit trail (the confirmation disclaimer shows the service account, not the real actor)

### Recommended architecture

```
Browser (user) → AP3 Dashboard (pod) → GitHub API  (with user's token)
                                     → Jenkins API (with user's token)
                                     → Platform git repo (service account)
```

The dashboard pod holds:
- A **service account** git token for committing to the platform repo (read/write on `envs/`)
- **No** GitHub/Jenkins tokens of its own

Each user provides their own tokens when they log in. These are stored encrypted in a secret manager — not in the pod's environment.

### Implementation design (per-user tokens in the dashboard)

#### 1. User registration flow

```
User opens dashboard
  → POST /api/auth/register { github_token, jenkins_user, jenkins_token }
  → Server validates tokens (calls GitHub /user and Jenkins /me)
  → Stores encrypted in secret backend (Vault / AWS Secrets Manager / k8s Secret)
  → Issues a session JWT signed with AP3_SESSION_SECRET
  → Returns JWT in HttpOnly cookie
```

#### 2. Session token (JWT)

```json
{
  "sub": "jane.doe",
  "github_login": "janedoe",
  "github_name": "Jane Doe",
  "jenkins_user": "jane.doe",
  "secret_ref": "ap3/users/janedoe",
  "exp": 1234567890
}
```

The `secret_ref` is a path in the secret manager where the user's tokens are stored. The server fetches them on each request using the **pod's service account** credentials (IRSA on EKS, Workload Identity on GKE/OKE, or a Vault AppRole).

#### 3. Per-request token resolution

```python
# Pseudocode — how each API call would resolve tokens
def get_user_tokens(session: JWT) -> tuple[str, str, str]:
    secret = vault.get(session.secret_ref)  # or AWS SM, or k8s Secret
    return (
        secret["github_token"],
        secret["jenkins_user"],
        secret["jenkins_token"],
    )
```

#### 4. Secret manager options

| Option | When to use | Notes |
|---|---|---|
| **HashiCorp Vault** | On-premises / OpenShift | Vault Agent Injector auto-injects tokens; KV secrets engine |
| **AWS Secrets Manager** | AWS / EKS | IRSA gives the pod permission to read user secrets without static credentials |
| **Kubernetes Secrets** | Simple setups | Encrypted at rest (with etcd encryption). Not a full secret manager but acceptable for small teams |
| **OpenShift Secrets** | OpenShift | Same as k8s Secrets, managed via oc/ArgoCD |

#### 5. Platform git repo — service account credentials

The git commits to `envs/` (created by `env_manager._git_commit()`) should use a **dedicated service account**:

```bash
# In the pod — set once via ConfigMap/Secret
GIT_AUTHOR_NAME="AP3 Dashboard Service"
GIT_AUTHOR_EMAIL="ap3-dashboard@my-org.com"
```

The service account has push access to the platform repo. Individual actors are still recorded in `_meta.updated_by` from the session JWT — the git author is just the service identity.

### Minimum viable implementation for the dashboard-as-service

The current AP3 dashboard does not implement session management. To deploy it as a shared service today, the simplest safe approach is:

1. Use **network-level access control** — deploy the dashboard inside the cluster, accessible only on the internal network (no public ingress)
2. Protect with **OpenShift OAuth** or an **nginx sidecar with basic auth**
3. Use a single shared service account for GitHub/Jenkins — acceptable when the team is small and the audit trail via git is sufficient
4. Graduate to per-user tokens (Vault/AWS SM) when the team grows or audit requirements tighten

### Environment variables for the dashboard pod

```yaml
# Kubernetes Deployment env section
env:
  # Platform git service account
  - name: GIT_AUTHOR_NAME
    value: "AP3 Dashboard"
  - name: GIT_AUTHOR_EMAIL
    value: "ap3@my-org.com"

  # Shared service account tokens (start simple — one set for everyone)
  - name: GITHUB_TOKEN
    valueFrom:
      secretKeyRef:
        name: ap3-platform-secrets
        key: github_token
  - name: JENKINS_USER
    valueFrom:
      secretKeyRef:
        name: ap3-platform-secrets
        key: jenkins_user
  - name: JENKINS_TOKEN
    valueFrom:
      secretKeyRef:
        name: ap3-platform-secrets
        key: jenkins_token

  # Session signing secret (when per-user auth is implemented)
  # - name: AP3_SESSION_SECRET
  #   valueFrom:
  #     secretKeyRef:
  #       name: ap3-platform-secrets
  #       key: session_secret
```

---

## Summary: which approach for which situation?

| Situation | Token management | Notes |
|---|---|---|
| Solo developer, local CLI | `.env` file | Simple, sufficient |
| Small team, local dashboard | `.env` on each machine | Each dev has their own tokens |
| Small team, shared dashboard | Single service account in k8s Secret | Simple; everyone appears as the service account in GitHub/Jenkins |
| Larger team, shared dashboard | Per-user tokens in Vault or AWS SM | Full audit trail; rotate independently; AP3 session JWT |
| CI pipeline (Jenkins) | Jenkins Credentials | Inject via `withCredentials` block |
