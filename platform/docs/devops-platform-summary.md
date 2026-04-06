# DevOps Platform — Technical & Workflow Summary

## 1. Technical Approach

### Jenkins Pipeline as Code

- The **Jenkinsfile lives in the root of each service repository**, version-controlled alongside application code
- In a central DevOps platform model, a **Shared Library** (separate Git repo owned exclusively by the DevOps team) holds all pipeline logic
- Service repos contain only a **thin Jenkinsfile** that calls the shared library:

```groovy
@Library('central-devops-lib@v2.1') _
standardPipeline(
  language: 'java',
  deployEnv: 'prod'
)
```

- The library version is pinned (`@v2.1`) — service teams cannot silently pull changes; upgrades are controlled by DevOps

---

### Platform Onboarding Control

To be part of the platform, a service must:

1. Have a Jenkinsfile in their repo referencing the shared library
2. Be registered in Jenkins (job created via Jenkins API by the DevOps team)

The DevOps team controls both gates — service teams cannot self-register.

Onboarding automation (via scripts) can:
- Create the GitHub repo via API
- Commit a templated Jenkinsfile via API
- Commit a CODEOWNERS file via API
- Apply branch protection rules via API
- Register the Jenkins job via Jenkins API

---

### Branch Protection (GitHub API)

Branch protection is a **server-side platform feature**, not a native Git feature. Git itself has no concept of it — enforcement happens via server-side hooks on GitHub/GitLab/Bitbucket.

Key GitHub API endpoints for automation:

```
POST /orgs/{org}/repos                                    # Create repo
PUT  /repos/{owner}/{repo}/branches/{branch}/protection   # Set branch rules
PUT  /repos/{owner}/{repo}/contents/.github/CODEOWNERS    # Push CODEOWNERS file
```

Example branch protection payload:

```json
{
  "required_pull_request_reviews": {
    "required_approving_review_count": 1,
    "dismiss_stale_reviews": true
  },
  "required_status_checks": {
    "strict": true,
    "contexts": ["jenkins-pipeline"]
  },
  "enforce_admins": true
}
```

`strict: true` forces the PR branch to be up to date with the target branch before merge — ensuring code is always tested against the latest state of the target.

**CODEOWNERS** — ensures any PR touching the Jenkinsfile requires DevOps team approval, even though the file lives in the service repo:

```
# .github/CODEOWNERS
Jenkinsfile @devops-team
```

Auth: GitHub token with `repo` + `admin:org` scopes, or preferably a **GitHub App** with fine-grained permissions.

---

### Jenkins + SonarQube Integration

- **GitHub → Jenkins**: webhook fires on PR creation and each new push
- **Jenkins → SonarQube**: scanner runs analysis inside the pipeline
- **SonarQube → Jenkins**: quality gate result returned, Jenkins waits via `waitForQualityGate()`
- **Jenkins → GitHub**: commit status reported (✅ or ❌), GitHub enforces merge block

Pipeline stages in the shared library (non-bypassable by service teams):

```groovy
stage('Sonar Analysis') {
    withSonarQubeEnv('sonar-server') {
        sh 'mvn sonar:sonar'
    }
}
stage('Quality Gate') {
    waitForQualityGate abortPipeline: true
}
stage('Manual Review') {
    timeout(time: 24, unit: 'HOURS') {
        slackSend channel: '#devops-approvals',
                  message: "Approval needed: ${env.JOB_NAME} - ${env.BUILD_URL}input"
        input message: 'Approve?', submitter: 'devops-team'
    }
}
```

Manual approval gate: only designated approvers (`submitter`) can unblock the pipeline. A `timeout` prevents hanging executors.

---

### Branching Strategy & Release Management

- Branch protection rules apply **per target branch**, not just main. Each level in the pipeline (develop → release → main) can have its own ruleset with increasing strictness.
- **The core problem with shared integration branches**: once a feature is merged to develop, it cannot be easily removed without a revert commit. Cherry-picking is the escape hatch but becomes painful with complex commit histories.

Three strategies to address this:

| Strategy | Description | Tradeoff |
|---|---|---|
| **Git revert** | Revert the merge commit of the unwanted feature | Creates noise, revert must be re-reverted later |
| **Feature flags** | Feature merged but disabled by config at runtime | Requires flag infrastructure, PO controls enablement |
| **Ephemeral environments** | Feature branches deployed independently for PO validation before merging to develop | Requires infrastructure investment, cleanest outcome |

The ephemeral environment approach (preview environments per PR/branch) is the modern standard — develop only ever contains PO-validated features.

---

### Event-Driven Architecture & Webhook Resilience

> ⚠️ **Architecture Recommendation** — This section describes a critical production concern. Webhooks alone are not sufficient for a reliable platform. The pattern below is strongly recommended.

#### The Problem with Raw Webhooks

Webhooks are **fire and forget**. If your control tower server is down when GitHub fires a webhook, the event is lost. GitHub does retry failed deliveries, but only for a few hours with limited attempts — not reliable enough for a production platform.

#### Recommended Architecture: Queue-Based Event Processing

Never let GitHub call your control tower directly. Introduce a **durable message queue** as a buffer:

```
GitHub ──webhook──→ [Lightweight Receiver] ──→ [Message Queue] ──→ [Control Tower]
                     (always up, one job:          (durable,            (consumes
                      write to queue)               persistent)          when ready)
```

The receiver is a tiny, highly available service whose only job is to acknowledge GitHub and write the event to the queue. The control tower consumes from the queue at its own pace and can restart, deploy, or crash without losing a single event.

**Recommended queue technologies:**

| Option | Best for |
|---|---|
| **RabbitMQ** | Self-hosted, moderate scale |
| **Apache Kafka** | High volume, event replay needed |
| **AWS SQS** | Cloud-native, managed, simple |
| **Azure Service Bus** | Azure ecosystem |

#### The Remaining Gap — Receiver Downtime

Even with a queue, if the receiver itself is briefly unavailable, events can be missed. The safety net for this is **reconciliation polling**:

```
Every X minutes (cron job):
  → Query GitHub API  for recent PR / push events
  → Query Jenkins API for recent build statuses
  → Compare against internal control tower state
  → Detect and reprocess anything missed
```

#### Full Resilient Architecture

```
                    ┌─────────────────────────────────────────┐
                    │           REAL-TIME PATH                 │
                    │                                          │
GitHub ──webhook──→ [Receiver] ──→ [Message Queue] ──→ [Control Tower]
Jira   ──webhook──→ [Receiver] ──/                         │
Jenkins──webhook──→ [Receiver] ──/                         │
                    │                                          │
                    │           CATCH-UP PATH                  │
                    │                                          │
                    [Scheduler/Cron] ──polling every X min ───┘
                    (GitHub API, Jenkins API, Sonar API)
                    └─────────────────────────────────────────┘
```

**Three-layer resilience:**
- **Webhooks** → real-time, low latency, primary path
- **Message queue** → durability and decoupling, survives control tower downtime
- **Polling reconciliation** → safety net, catches anything that slipped through

This is a standard **event-driven architecture** pattern. Skipping the queue layer is the most common mistake when building internal platforms — it works fine in development and fails silently in production.

---

### IDP Control Tower (Advanced)

A central orchestration service can aggregate data from all platform APIs to provide full feature lifecycle traceability:

```
[Jira webhook] ──→ [Control Tower API] ←── [GitHub webhook]
                          │
                          ├── GitHub API   (create PRs, revert, branch status)
                          ├── Jenkins API  (trigger builds, get status)
                          ├── Sonar API    (quality gate status)
                          └── Notifications (Slack / Teams / Email)
```

The **naming convention** is the correlation key:

```
branch:   feature/JIRA-123-my-feature
PR title: [JIRA-123] My feature
```

This links a Jira ticket → GitHub branch → PR → Jenkins build → Sonar report in one view.

Automation flows enabled:
- **Revert flow**: PO marks ticket "not for release" → control tower detects feature on develop → creates revert PR automatically → notifies DevOps for approval → updates Jira
- **Remerge flow**: PO marks ticket "ready for next release" → control tower detects revert → creates remerge PR from rebased branch → reruns full pipeline → updates Jira

Open source IDP frameworks: **Backstage** (Spotify), **Port**, **Cortex**.

---

### Platform as a Product

The DevOps team and its tools are themselves services on the platform — they follow the same rules they enforce on others. This includes:

- The control tower onboarded via the standard onboarding process
- The control tower pipeline validated by Sonar, going through the same approval gates
- The shared library repo protected by the same branch rules

This is called **Platform as a Product** — the DevOps team treats internal teams as customers, with their own roadmap, SLAs, and versioning. It closes a credibility gap: a team that bypasses its own rules loses trust. A team that ships through the same gates demonstrates the platform is production grade, not governance theater.

---

### Bootstrap Pipeline

> ⚠️ **Architectural Exception** — This is a known and intentional deviation from the standard platform flow. It must be documented and access-controlled carefully.

#### The Circular Dependency Problem

The shared library is a dependency of every pipeline on the platform — including the pipeline that would normally validate and release it:

```
Shared library v2 has a bug
→ Need a pipeline to test and release the fix
→ That pipeline uses... shared library v2
→ Cannot use a broken library to validate itself
```

#### The Bootstrap Pipeline Solution

A **minimal, standalone Jenkinsfile** lives inside the shared library repo itself under `bootstrap/Jenkinsfile`. It has **zero dependency on the shared library** — no `@Library` reference, plain Jenkins DSL only:

```groovy
// bootstrap/Jenkinsfile — no @Library, pure Jenkins DSL
pipeline {
    agent any
    stages {
        stage('Syntax Check') {
            steps {
                sh 'groovy -e "import groovy.transform.*"'
            }
        }
        stage('Unit Tests') {
            steps {
                sh 'mvn test'   // JenkinsPipelineUnit test suite
            }
        }
        stage('Release') {
            steps {
                sh 'git tag v${VERSION}'
                sh 'git push origin v${VERSION}'
            }
        }
    }
}
```

#### Key Design Principles

- **Kept deliberately simple** — no fancy stages, no Sonar, no manual gates beyond human PR review. Just enough to validate and release safely
- **Separate Jenkins job** — not created by the onboarding automation. Manually registered once by a senior DevOps engineer
- **Restricted access** — only library maintainers can trigger it. No service team access
- **Versioned releases via Git tags** — the output is a tag (`v2.2`), which is what all service pipelines pin to via `@Library('central-devops-lib@v2.2')`
- **Own test suite** — the shared library must have unit tests for its Groovy code using **JenkinsPipelineUnit**, validated by the bootstrap pipeline

#### Shared Library Release Flow

```
DevOps dev makes change to shared library
→ PR opened on library repo
→ Bootstrap pipeline runs (syntax check + unit tests)
→ Senior DevOps engineer approves PR manually
→ Merge to main
→ Bootstrap pipeline tags the release (v2.2)
→ Services opt in by updating their @Library pin when ready
```

Teams are **not forced onto the new version immediately** — they update their pin on their own schedule, giving a controlled, non-breaking rollout across the platform.

#### Summary: Two Classes of Pipeline on the Platform

| Pipeline type | Uses shared library | Created by | Purpose |
|---|---|---|---|
| **Standard service pipeline** | Yes (`@Library`) | Onboarding automation (API) | All platform services |
| **Bootstrap pipeline** | No (plain DSL) | Manual, senior DevOps only | Validate & release the shared library itself |

The bootstrap pipeline is the only legitimate exception to the "everything uses the shared library" rule, and its existence should be explicitly documented in the platform architecture decision records (ADRs).

---

### Jenkins Agents & Dynamic Provisioning on OpenShift

#### Static vs Dynamic Agents

The Jenkins **controller** orchestrates pipelines but should never run build workloads itself — it is too critical to risk overloading. **Agents** are the worker nodes that execute pipeline stages.

Two modes:

| Mode | Description | Tradeoff |
|---|---|---|
| **Static agents** | Pre-provisioned VMs registered permanently in Jenkins | Always available, idle cost, manual maintenance |
| **Dynamic agents** | Pods spun up on demand on Kubernetes/OpenShift, destroyed after job | Zero idle cost, perfect isolation, scales automatically |

Dynamic provisioning on OpenShift is the **recommended pattern** for a central DevOps platform.

---

#### How Dynamic Agent Provisioning Works

Jenkins uses the **Kubernetes plugin** to call the OpenShift API and create a pod per build:

```
Jenkins Controller (external)
  │
  └──HTTPS──→ OpenShift API
                └── creates Agent Pod on demand
                      └── Pod ──outbound JNLP──→ Jenkins Controller
                            └── executes build stages
                                  └── Pod destroyed on completion
```

The pod initiates the connection back to Jenkins — same outbound-only security pattern as GitHub self-hosted runners. No inbound ports needed on the Jenkins controller.

---

#### Pipeline Declaration

Pod templates are defined **in the shared library** — service teams cannot change what tools or images are available:

```groovy
// Defined in shared library — service teams have no control over this
pipeline {
    agent {
        kubernetes {
            yaml '''
                apiVersion: v1
                kind: Pod
                spec:
                  containers:
                  - name: maven
                    image: maven:3.9-eclipse-temurin-17
                    command: [sleep, infinity]
                  - name: sonar-scanner
                    image: sonarsource/sonar-scanner-cli
                    command: [sleep, infinity]
            '''
        }
    }
    stages {
        stage('Build') {
            steps {
                container('maven') {
                    sh 'mvn package'
                }
            }
        }
        stage('Sonar') {
            steps {
                container('sonar-scanner') {
                    sh 'sonar-scanner'
                }
            }
        }
    }
}
```

Each stage runs in the right container — all inside the same pod, created fresh per build.

---

#### Authentication — Jenkins to OpenShift

Jenkins needs credentials to call the OpenShift API. Recommended approach is a **dedicated Service Account** with minimum permissions:

```
OpenShift Service Account
  └── bound to a Role: create/get/delete pods in build namespace only
        └── token stored as Jenkins credential
              └── Kubernetes plugin uses token to call OpenShift API
```

The service account should have no permissions beyond the build namespace — principle of least privilege.

---

#### OpenShift Specific Considerations

OpenShift is stricter than vanilla Kubernetes by default:

- **Security Context Constraints (SCC)** — build pods must declare required privileges. Typically need a custom SCC or `anyuid`
- **Internal image registry** — build images should be hosted internally so pods can pull without reaching the internet
- **Resource quotas** — apply to the build namespace to prevent runaway builds consuming cluster resources:

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: jenkins-build-quota
  namespace: jenkins-builds
spec:
  hard:
    pods: "20"                 # max concurrent build pods
    requests.cpu: "10"
    requests.memory: 20Gi
    limits.cpu: "20"
    limits.memory: 40Gi
```

---

#### Full Infrastructure Picture

```
Your network
  │
  ├── [Jenkins Controller] ──HTTPS──→ OpenShift API
  │         │                           └── spins up Agent Pods per build
  │         │                                 └── pods ──JNLP──→ Jenkins Controller
  │         │
  │         └── delegates lightweight checks to ↓
  │
  ├── [GitHub Self-hosted Runner] ──443──→ GitHub (outbound only)
  │       └── PR title validation, ticket reference checks
  │
  ├── [SonarQube]    ←── called by agent pods
  ├── [Agility API]  ←── called by agent pods / runner
  └── [Internal services] ←── all reachable from within the network
```

#### Benefits for Your Platform

| Benefit | Detail |
|---|---|
| **No idle agents** | Pods exist only during builds, zero cost at rest |
| **Perfect isolation** | Each build gets a fresh environment, no contamination |
| **Scalability** | OpenShift scales pods automatically, no manual agent management |
| **Flexible tooling** | Each pipeline declares its own container image — Java, Node, Python |
| **Centrally controlled** | Pod templates defined in shared library, service teams cannot modify them |

---

## 2. Dev Workflow

### PR / MR Concepts

- A **PR/MR** is attached to a **branch**, not a commit. It stays open until merged or closed.
- Each new **push (commit) to the branch** triggers the webhook and reruns the pipeline. The PR status always reflects the **latest run**.
- `dismiss_stale_reviews: true` ensures that a new push after approval invalidates the previous review — preventing approved-then-tampered code from slipping through.

---

### Full PR Lifecycle

```
1.  Dev creates feature branch from develop (or target branch)
2.  Dev pushes code → opens PR targeting develop (or release)
3.  GitHub webhook fires → Jenkins pipeline triggered
4.  Jenkins runs: tests → Sonar analysis → quality gate
5.  If Sonar FAILS:
      → Jenkins reports ❌ to GitHub
      → GitHub blocks merge
      → Dev consults SonarQube for details, fixes locally, pushes again
      → Pipeline reruns automatically (back to step 4)
6.  If Sonar PASSES:
      → Notification sent to reviewers (Slack/email)
      → Manual approval gate opens in Jenkins
      → Approver reviews and clicks approve in Jenkins UI
      → Jenkins reports ✅ to GitHub
7.  GitHub allows merge (if strict: true, branch must also be up to date with target)
8.  If branch is out of date:
      git fetch origin
      git rebase origin/<target-branch>
      git push --force-with-lease
      → Pipeline reruns on rebased branch
9.  Merge allowed → PR merged
```

---

### Dev Tips for Faster Feedback

- Install **SonarLint** in VS Code / IntelliJ — runs the same Sonar rules locally in real time, catches issues before pushing
- Keep feature branches **short-lived and small** — reduces rebase conflicts and integration risk
- Use the ticket ID in branch names (`feature/JIRA-123-...`) for full traceability in the control tower

---

### Key Control Points Summary

| Control | Owner | Mechanism |
|---|---|---|
| Who gets onboarded | DevOps | Jenkins job created via API |
| What the pipeline does | DevOps | Shared library (restricted repo) |
| Jenkinsfile changes | DevOps | CODEOWNERS + branch protection |
| Library version | DevOps | Pinned version tag in Jenkinsfile |
| Merge allowed | Platform | GitHub branch protection + status checks |
| Manual approval | DevOps / Tech leads | Jenkins `input` step with `submitter` |
| Feature lifecycle tracking | Control tower | Jira + GitHub + Jenkins + Sonar APIs |
