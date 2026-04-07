# create.ps1 — Build and start the AP3 local test environment (Windows / PowerShell)
#
# Services started:
#   Gitea      :3000  — local GitHub (Git hosting + compatible REST API)
#   SonarQube  :9000  — code quality gate
#   Registry   :5000  — local container registry (simulates Artifactory)
#   Jenkins    :8080  — CI/CD (Kubernetes plugin → k3d build pods)
#   k3d              — lightweight Kubernetes
#
# Requirements:
#   - Docker Desktop (WSL2 backend enabled)
#   - k3d   (https://k3d.io  — winget install k3d.k3d)
#   - kubectl (winget install Kubernetes.kubectl)
#   - python3 (winget install Python.Python.3)
#   - curl.exe (built into Windows 10 1803+; also bundled with Git)
#
# Idempotent: safe to re-run after a partial failure.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File testenv\create.ps1

#Requires -Version 5.1

$ErrorActionPreference = 'Stop'
$SCRIPT_DIR    = $PSScriptRoot
$ENV_FILE      = "$SCRIPT_DIR\.env"
$USERS_FILE    = "$SCRIPT_DIR\.users"
$CLUSTER_NAME  = "ap3"
$K3D_API_PORT  = "6550"
$BUILDS_NS     = "jenkins-builds"

# ─── Helpers ──────────────────────────────────────────────────────────────────

function info($msg)  { Write-Host "[INFO]  $msg" -ForegroundColor Cyan }
function ok($msg)    { Write-Host "[ OK ]  $msg" -ForegroundColor Green }
function warn($msg)  { Write-Host "[WARN]  $msg" -ForegroundColor Yellow }
function die($msg)   { Write-Host "[ERR ]  $msg" -ForegroundColor Red; exit 1 }
function step($msg)  { Write-Host "`n══ $msg ══" -ForegroundColor Blue }

function New-RandomHex([int]$bytes) {
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $buf = [byte[]]::new($bytes)
    $rng.GetBytes($buf)
    return ($buf | ForEach-Object { $_.ToString('x2') }) -join ''
}

function Read-EnvFile([string]$Path) {
    if (-not (Test-Path $Path)) { return }
    Get-Content $Path | Where-Object { $_ -match '^[^#\s]\w*=' } | ForEach-Object {
        $idx = $_.IndexOf('=')
        $k   = $_.Substring(0, $idx).Trim()
        $v   = $_.Substring($idx + 1).Trim()
        Set-Item -Path "Env:$k" -Value $v
    }
}

function Update-EnvFile([string]$Path, [string]$Key, [string]$Value) {
    if (Test-Path $Path) {
        $lines = Get-Content $Path
        $found = $false
        $lines = $lines | ForEach-Object {
            if ($_ -match "^$Key=") { "$Key=$Value"; $found = $true } else { $_ }
        }
        if (-not $found) { $lines += "$Key=$Value" }
        $lines | Set-Content -Path $Path -Encoding UTF8
    } else {
        "$Key=$Value" | Set-Content -Path $Path -Encoding UTF8
    }
}

function Invoke-Dc {
    $dcArgs = @("--env-file", $ENV_FILE, "--env-file", $USERS_FILE, "-f", "$SCRIPT_DIR\docker-compose.yml")
    & docker compose @dcArgs @args
    if ($LASTEXITCODE -ne 0) { throw "docker compose exited with code $LASTEXITCODE" }
}

function Wait-Http([string]$Url, [string]$Label, [int]$Tries = 60, [int]$DelaySec = 5) {
    info "Waiting for $Label..."
    for ($i = 1; $i -le $Tries; $i++) {
        & curl.exe -sf --max-time 5 $Url 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { ok "$Label responded"; return }
        Write-Host -NoNewline "."
        Start-Sleep $DelaySec
    }
    Write-Host ""
    die "$Label did not become available after $($Tries * $DelaySec)s"
}

function Test-PortFree([int]$Port, [string]$Use) {
    # Use Get-NetTCPConnection (PS 4+) for reliable port detection
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($conn) {
        $pid = $conn | Select-Object -First 1 -ExpandProperty OwningProcess
        $proc = (Get-Process -Id $pid -ErrorAction SilentlyContinue)?.Name ?? "unknown"
        $script:BLOCKING += "Port $Port/tcp already in use (process: $proc, PID: $pid) — needed for $Use`n    Stop with: Stop-Process -Id $pid"
        warn "Port $Port in use ($proc PID $pid) — needed for $Use"
    } else {
        ok "Port $Port free  ($Use)"
    }
}

# ═══════════════════════════════════════════════════════════════════════════════
# PRE-FLIGHT CHECKS
# ═══════════════════════════════════════════════════════════════════════════════

step "Pre-flight checks"

$BLOCKING   = @()
$AUTO_FIXED = @()

function Require-Tool([string]$Name, [string]$Fix) {
    if (Get-Command $Name -ErrorAction SilentlyContinue) {
        ok "$Name"
    } else {
        warn "MISSING: $Name"
        $script:BLOCKING += "${Name} not found|$Fix"
    }
}

# ── Required tools ────────────────────────────────────────────────────────────

Require-Tool "docker"   "winget install Docker.DockerDesktop"
Require-Tool "curl.exe" "curl.exe is built into Windows 10 1803+; or install Git for Windows"
Require-Tool "python3"  "winget install Python.Python.3"

# ── Docker daemon ─────────────────────────────────────────────────────────────

info "Checking Docker daemon..."
& docker info 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    $BLOCKING += "Docker daemon not running|Start Docker Desktop and ensure the WSL2 backend is enabled"
    warn "Docker daemon not accessible"
} else {
    ok "Docker daemon accessible"
}

# ── WSL2 vm.max_map_count (SonarQube/Elasticsearch) ──────────────────────────

info "Checking vm.max_map_count..."
try {
    $currentMap = [int](wsl -d docker-desktop -- sysctl -n vm.max_map_count 2>$null)
    if ($currentMap -lt 524288) {
        info "  Applying vm.max_map_count=524288 (WSL2)..."
        wsl -d docker-desktop -- sysctl -w vm.max_map_count=524288 2>$null
        $AUTO_FIXED += "vm.max_map_count=524288 applied via WSL2 (resets on Docker Desktop restart — add to ~/.wslconfig to persist)"
        ok "vm.max_map_count set (current session)"
    } else {
        ok "vm.max_map_count = $currentMap (>= 524288)"
    }
} catch {
    warn "Could not set vm.max_map_count via WSL2 — SonarQube may fail to start"
    warn "  Fix: wsl -d docker-desktop -- sysctl -w vm.max_map_count=524288"
}

# ── Required ports ────────────────────────────────────────────────────────────

Test-PortFree 3000  "Gitea"
Test-PortFree 5000  "Registry"
Test-PortFree 8080  "Jenkins UI"
Test-PortFree 9000  "SonarQube"
Test-PortFree 50000 "Jenkins JNLP"
$clusterExists = (& k3d cluster list 2>$null) -match "(?m)^$CLUSTER_NAME\s"
if (-not $clusterExists) {
    Test-PortFree ([int]$K3D_API_PORT) "k3d Kubernetes API"
} else {
    ok "Port $K3D_API_PORT in use by existing k3d cluster '$CLUSTER_NAME' (expected)"
}

# ── k3d + kubectl (install automatically if absent) ──────────────────────────

if (-not (Get-Command k3d -ErrorAction SilentlyContinue)) {
    info "k3d not found — attempting install via winget..."
    & winget install k3d.k3d --silent 2>$null
    if ($LASTEXITCODE -eq 0) {
        $AUTO_FIXED += "k3d installed via winget"
        ok "k3d installed"
    } else {
        $BLOCKING += "k3d not found|winget install k3d.k3d`n    or: https://k3d.io/#installation"
    }
} else {
    ok "k3d: $((& k3d version 2>$null | Select-Object -First 1) ?? 'installed')"
}

if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
    info "kubectl not found — attempting install via winget..."
    & winget install Kubernetes.kubectl --silent 2>$null
    if ($LASTEXITCODE -eq 0) {
        $AUTO_FIXED += "kubectl installed via winget"
        ok "kubectl installed"
    } else {
        $BLOCKING += "kubectl not found|winget install Kubernetes.kubectl"
    }
} else {
    ok "kubectl: $((& kubectl version --client 2>$null | Select-Object -First 1) ?? 'installed')"
}

# ── Show results ──────────────────────────────────────────────────────────────

if ($AUTO_FIXED.Count -gt 0) {
    Write-Host ""
    info "Auto-fixed $($AUTO_FIXED.Count) issue(s):"
    $AUTO_FIXED | ForEach-Object { Write-Host "    ✓ $_" }
}

if ($BLOCKING.Count -gt 0) {
    Write-Host ""
    Write-Host "┌─────────────────────────────────────────────────────────────────┐"
    Write-Host "│       Pre-flight FAILED — fix the issues below and re-run      │"
    Write-Host "└─────────────────────────────────────────────────────────────────┘"
    $n = 1
    foreach ($entry in $BLOCKING) {
        $parts = $entry -split '\|', 2
        Write-Host ""
        Write-Host "  [$n] $($parts[0])" -ForegroundColor Red
        if ($parts.Count -gt 1) {
            $parts[1] -split "`n" | Where-Object { $_ } | ForEach-Object {
                Write-Host "      `$ $_" -ForegroundColor Yellow
            }
        }
        $n++
    }
    Write-Host ""
    Write-Host "  Re-run after fixing:  powershell -ExecutionPolicy Bypass -File testenv\create.ps1"
    Write-Host ""
    exit 1
}

ok "All pre-flight checks passed"

# ─── Generate credentials ─────────────────────────────────────────────────────

step "Credentials"

if (Test-Path $ENV_FILE)   { Read-EnvFile $ENV_FILE }
if (Test-Path $USERS_FILE) { Read-EnvFile $USERS_FILE }

if (-not $env:POSTGRES_PASSWORD) {
    $env:POSTGRES_PASSWORD      = New-RandomHex 16
    $env:SONAR_ADMIN_PASSWORD   = New-RandomHex 12
    $env:JENKINS_ADMIN_USER     = "admin"
    $env:JENKINS_ADMIN_PASSWORD = New-RandomHex 12
    $env:GITEA_ADMIN_PASSWORD   = New-RandomHex 12
    $env:REGISTRY_PASSWORD      = New-RandomHex 12
    info "Generated new credentials"
} else {
    ok "Re-using existing credentials from .env / .users"
}

$timestamp = (Get-Date -Format "yyyy-MM-ddTHH:mm:sszzz")

@"
# AP3 Test Environment — $timestamp
# ─────────────────────────────────────────────────────────────────────────────
# API tokens and service URLs.  DO NOT COMMIT.
# After create.ps1 completes:
#   Get-Content testenv\.env | ForEach-Object { `$k, `$v = `$_ -split '=', 2; Set-Item "Env:`$k" `$v }

SONARQUBE_TOKEN=$($env:SONARQUBE_TOKEN ?? '__PENDING__')
JENKINS_USER=$($env:JENKINS_ADMIN_USER ?? 'admin')
JENKINS_TOKEN=$($env:JENKINS_TOKEN ?? '__PENDING__')
GITEA_TOKEN=$($env:GITEA_TOKEN ?? '__PENDING__')
# GITHUB_TOKEN is set to the Gitea token so platform scripts work without changes
GITHUB_TOKEN=$($env:GITHUB_TOKEN ?? '__PENDING__')
K8S_API_URL=$($env:K8S_API_URL ?? '__PENDING__')
K8S_SA_TOKEN=$($env:K8S_SA_TOKEN ?? '__PENDING__')

# Service URLs (from the host)
JENKINS_URL=http://localhost:8080
SONARQUBE_URL=http://localhost:9000
GITEA_URL=http://localhost:3000
REGISTRY_URL=localhost:5000
# Org and shared-lib repo name (used by casc/jenkins.yaml via docker-compose)
GITEA_ORG=ap3
SHARED_LIB_REPO_NAME=jenkins-shared-lib
"@ | Set-Content -Path $ENV_FILE -Encoding UTF8

@"
# AP3 Test Environment — $timestamp
# ─────────────────────────────────────────────────────────────────────────────
# Admin usernames and passwords for manual web UI access.  DO NOT COMMIT.

POSTGRES_PASSWORD=$($env:POSTGRES_PASSWORD)
SONAR_ADMIN_PASSWORD=$($env:SONAR_ADMIN_PASSWORD)
JENKINS_ADMIN_USER=$($env:JENKINS_ADMIN_USER ?? 'admin')
JENKINS_ADMIN_PASSWORD=$($env:JENKINS_ADMIN_PASSWORD)
GITEA_ADMIN_USER=ap3admin
GITEA_ADMIN_PASSWORD=$($env:GITEA_ADMIN_PASSWORD ?? '__PENDING__')
REGISTRY_PASSWORD=$($env:REGISTRY_PASSWORD)
"@ | Set-Content -Path $USERS_FILE -Encoding UTF8

ok ".env (tokens) and .users (passwords) written"
Read-EnvFile $ENV_FILE
Read-EnvFile $USERS_FILE

Remove-Item "$SCRIPT_DIR\docker-compose.yml.bak"            -Force -ErrorAction SilentlyContinue
Remove-Item "$SCRIPT_DIR\jenkins\casc\jenkins.yaml.bak"     -Force -ErrorAction SilentlyContinue

# ─── k3d cluster ──────────────────────────────────────────────────────────────

step "k3d cluster"

$clusterExists = (& k3d cluster list 2>$null) -match "(?m)^$CLUSTER_NAME\s"
if ($clusterExists) {
    ok "Cluster '$CLUSTER_NAME' already exists"
    & k3d cluster start $CLUSTER_NAME 2>$null
} else {
    info "Creating k3d cluster '$CLUSTER_NAME' (~60-90s)..."
    & k3d cluster create $CLUSTER_NAME `
        --api-port  "0.0.0.0:${K3D_API_PORT}" `
        --k3s-arg   "--disable=traefik@server:0" `
        --wait `
        --timeout   180s
    if ($LASTEXITCODE -ne 0) { die "k3d cluster create failed" }
    ok "k3d cluster created"
}

& k3d kubeconfig merge $CLUSTER_NAME --kubeconfig-switch-context
ok "kubeconfig → k3d-$CLUSTER_NAME"

# ─── k8s ServiceAccount ───────────────────────────────────────────────────────

step "Kubernetes ServiceAccount"

& kubectl apply -f "$SCRIPT_DIR\k8s\jenkins-sa.yaml"
ok "SA resources applied"

step "Platform namespaces"
& kubectl apply -f "$SCRIPT_DIR\k8s\platform-namespaces.yaml"
ok "platform-dev / platform-val / platform-prod ready"

info "Waiting for SA token..."
$K8S_SA_TOKEN = ""
for ($i = 1; $i -le 20; $i++) {
    $encoded = & kubectl -n $BUILDS_NS get secret jenkins-sa-token `
        -o jsonpath='{.data.token}' 2>$null
    if ($encoded) {
        $K8S_SA_TOKEN = [System.Text.Encoding]::UTF8.GetString(
            [System.Convert]::FromBase64String($encoded)
        )
        break
    }
    Start-Sleep 3
}
if (-not $K8S_SA_TOKEN) {
    die "SA token never populated. Check: kubectl -n $BUILDS_NS describe secret jenkins-sa-token"
}

Update-EnvFile $ENV_FILE "K8S_SA_TOKEN" $K8S_SA_TOKEN
ok "SA token retrieved"

$K8S_API_URL = "https://k3d-${CLUSTER_NAME}-serverlb:6443"
Update-EnvFile $ENV_FILE "K8S_API_URL" $K8S_API_URL
ok "k3d API (via k3d network): $K8S_API_URL"
$env:K8S_API_URL = $K8S_API_URL

Read-EnvFile $ENV_FILE

# ─── Phase 1: Gitea + SonarQube + Registry ────────────────────────────────────

step "Gitea + SonarQube + Registry"

Invoke-Dc up -d postgres sonarqube registry gitea

# ── Gitea ─────────────────────────────────────────────────────────────────────

Wait-Http "http://localhost:3000" "Gitea HTTP" 30 3

info "Waiting for Gitea API to be ready..."
for ($i = 1; $i -le 40; $i++) {
    & curl.exe -sf --max-time 3 "http://localhost:3000/api/v1/settings/api" 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { ok "Gitea API ready"; break }
    Write-Host -NoNewline "."
    Start-Sleep 3
}
Write-Host ""
& curl.exe -sf --max-time 3 "http://localhost:3000/api/v1/settings/api" 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { die "Gitea API never became ready. Check: docker compose logs gitea" }

# Validate stored token
if ($env:GITEA_TOKEN -and $env:GITEA_TOKEN -ne "__PENDING__") {
    $tokStatus = & curl.exe -sf -o NUL -w "%{http_code}" `
        -H "Authorization: token $($env:GITEA_TOKEN)" `
        "http://localhost:3000/api/v1/user" 2>$null
    if ($tokStatus -ne "200") {
        warn "Stored Gitea token is stale (HTTP $tokStatus) — will regenerate"
        $env:GITEA_TOKEN = "__PENDING__"
        Update-EnvFile $ENV_FILE "GITEA_TOKEN"  "__PENDING__"
        Update-EnvFile $ENV_FILE "GITHUB_TOKEN" "__PENDING__"
    }
}

if (-not $env:GITEA_TOKEN -or $env:GITEA_TOKEN -eq "__PENDING__") {
    info "Registering Gitea admin user 'ap3admin' via web form..."

    # Fetch signup page for CSRF token
    $signupHtml = & curl.exe -sc "$env:TEMP\ap3_gitea_jar" --max-time 10 `
        "http://localhost:3000/user/sign_up" 2>$null
    $giteaCsrf = if ($signupHtml -match 'name="_csrf"\s+value="([^"]+)"') { $Matches[1] }
                 elseif ($signupHtml -match 'content="([^"]+)"[^>]*name="_csrf"') { $Matches[1] }
                 else { "" }

    if ($giteaCsrf) {
        $signupStatus = & curl.exe -sb "$env:TEMP\ap3_gitea_jar" -c "$env:TEMP\ap3_gitea_jar" `
            -s --max-time 10 -o NUL -w "%{http_code}" `
            -X POST "http://localhost:3000/user/sign_up" `
            --data-urlencode "_csrf=$giteaCsrf" `
            --data-urlencode "user_name=ap3admin" `
            --data-urlencode "email=admin@ap3.local" `
            --data-urlencode "password=$($env:GITEA_ADMIN_PASSWORD)" `
            --data-urlencode "retype=$($env:GITEA_ADMIN_PASSWORD)" `
            -L 2>$null
        info "  signup HTTP status: $signupStatus"
    } else {
        warn "Could not extract CSRF token from Gitea signup page"
    }

    # Verify admin user
    & curl.exe -sf --max-time 5 -u "ap3admin:$($env:GITEA_ADMIN_PASSWORD)" `
        "http://localhost:3000/api/v1/user" 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        die "Could not create Gitea admin user. Check logs: docker compose logs gitea"
    }
    ok "Gitea admin user 'ap3admin' ready"

    # Delete stale token if any, then create fresh one
    & curl.exe -s --max-time 10 `
        -u "ap3admin:$($env:GITEA_ADMIN_PASSWORD)" -X DELETE `
        "http://localhost:3000/api/v1/users/ap3admin/tokens/ap3-platform" `
        2>$null | Out-Null

    $tokenResp = & curl.exe -s --max-time 10 `
        -u "ap3admin:$($env:GITEA_ADMIN_PASSWORD)" `
        -X POST "http://localhost:3000/api/v1/users/ap3admin/tokens" `
        -H "Content-Type: application/json" `
        -d '{
          "name": "ap3-platform",
          "scopes": [
            "write:repository", "read:repository",
            "write:organization", "read:organization",
            "write:user", "read:user",
            "write:admin", "read:admin",
            "write:issue", "read:issue",
            "read:misc"
          ]
        }' 2>$null
    $giteaToken = $tokenResp | & python3 -c "import sys,json; print(json.load(sys.stdin)['sha1'])" 2>$null
    if (-not $giteaToken) { die "Failed to create Gitea token. Response: $tokenResp" }

    # Create org
    & curl.exe -sf --max-time 10 `
        -H "Authorization: token $giteaToken" `
        -X POST "http://localhost:3000/api/v1/orgs" `
        -H "Content-Type: application/json" `
        -d '{"username":"ap3","visibility":"public","repo_admin_change_team_access":true}' `
        2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { info "  (org already exists)" }

    $env:GITEA_TOKEN   = $giteaToken
    $env:GITHUB_TOKEN  = $giteaToken
    Update-EnvFile $USERS_FILE "GITEA_ADMIN_PASSWORD" $env:GITEA_ADMIN_PASSWORD
    Update-EnvFile $ENV_FILE   "GITEA_TOKEN"          $giteaToken
    Update-EnvFile $ENV_FILE   "GITHUB_TOKEN"         $giteaToken
    ok "Gitea configured  (ap3admin / $($env:GITEA_ADMIN_PASSWORD))"
} else {
    ok "Gitea token already captured"
}

Read-EnvFile $ENV_FILE

# ── SonarQube ─────────────────────────────────────────────────────────────────

Wait-Http "http://localhost:9000/api/system/status" "SonarQube HTTP" 80 5

info "Waiting for SonarQube UP status..."
$sqStatus = ""
for ($i = 1; $i -le 40; $i++) {
    $sqResp = & curl.exe -sf "http://localhost:9000/api/system/status" 2>$null
    $sqStatus = $sqResp | & python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>$null
    if ($sqStatus -eq "UP") { ok "SonarQube: UP"; break }
    Write-Host -NoNewline "."
    Start-Sleep 5
}
if ($sqStatus -ne "UP") { die "SonarQube never reached UP. Check: docker compose logs sonarqube" }

# Change default admin password
$validResp = & curl.exe -sf --max-time 10 -u "admin:admin" `
    "http://localhost:9000/api/authentication/validate" 2>$null
if ($validResp -match '"valid":true') {
    info "Changing SonarQube admin password..."
    & curl.exe -sf --max-time 10 -u "admin:admin" `
        -X POST "http://localhost:9000/api/users/change_password" `
        --data-urlencode "login=admin" `
        --data-urlencode "password=$($env:SONAR_ADMIN_PASSWORD)" `
        --data-urlencode "previousPassword=admin" 2>$null | Out-Null
    ok "SonarQube admin password changed"
}

if (-not $env:SONARQUBE_TOKEN -or $env:SONARQUBE_TOKEN -eq "__PENDING__") {
    & curl.exe -sf --max-time 10 -u "admin:$($env:SONAR_ADMIN_PASSWORD)" -X POST `
        "http://localhost:9000/api/user_tokens/revoke" -d "name=jenkins-token" 2>$null | Out-Null

    $sqTokenResp = & curl.exe -sf --max-time 10 -u "admin:$($env:SONAR_ADMIN_PASSWORD)" -X POST `
        "http://localhost:9000/api/user_tokens/generate" `
        -d "name=jenkins-token&type=GLOBAL_ANALYSIS_TOKEN" 2>$null
    $sonarToken = $sqTokenResp | & python3 -c "import sys,json; print(json.load(sys.stdin)['token'])" 2>$null
    if (-not $sonarToken) { die "Failed to get SonarQube token. Response: $sqTokenResp" }
    $env:SONARQUBE_TOKEN = $sonarToken
    Update-EnvFile $ENV_FILE "SONARQUBE_TOKEN" $sonarToken
    ok "SonarQube token generated"
} else {
    ok "SonarQube token already captured"
}

Read-EnvFile $ENV_FILE

# ─── Gitea → k3d DNS registration ────────────────────────────────────────────

step "Gitea k3d DNS"

& docker network connect "k3d-$CLUSTER_NAME" gitea 2>$null
if ($LASTEXITCODE -eq 0) {
    ok "Gitea connected to k3d-$CLUSTER_NAME"
} else {
    ok "Gitea already on k3d-$CLUSTER_NAME network"
}

$inspectJson = & docker network inspect "k3d-$CLUSTER_NAME" 2>$null
$gitea_k3d_ip = $inspectJson | & python3 -c @"
import sys, json
data = json.load(sys.stdin)
net = data[0] if data else {}
containers = net.get('Containers') or net.get('containers') or {}
for c in containers.values():
    name = c.get('Name') or c.get('name', '')
    if name != 'gitea':
        continue
    ipv4 = c.get('IPv4Address', '')
    if ipv4:
        print(ipv4.split('/')[0]); break
    for iface in c.get('interfaces', {}).values():
        for s in iface.get('subnets', []):
            ip = s.get('ipnet', '')
            if ip:
                print(ip.split('/')[0]); break
    break
"@ 2>$null

if (-not $gitea_k3d_ip) {
    warn "Could not determine Gitea IP on k3d-$CLUSTER_NAME — DNS registration skipped."
    warn "k3d pods may not resolve 'gitea'. Re-run create.ps1 to retry (idempotent)."
} else {
    ok "Gitea IP on k3d-${CLUSTER_NAME}: $gitea_k3d_ip"

    # Register a headless Service + Endpoints in the jenkins-builds namespace so
    # that agent pods resolve 'gitea' immediately via kube-dns — no ConfigMap
    # file-sync latency, no CoreDNS restart needed. kubectl apply is idempotent.
    $manifest = @"
apiVersion: v1
kind: Service
metadata:
  name: gitea
spec:
  clusterIP: None
  ports:
  - name: http
    port: 3000
    protocol: TCP
    targetPort: 3000
---
apiVersion: v1
kind: Endpoints
metadata:
  name: gitea
subsets:
- addresses:
  - ip: $gitea_k3d_ip
  ports:
  - name: http
    port: 3000
    protocol: TCP
"@
    $manifest | & kubectl apply -n $BUILDS_NS -f -
    ok "gitea Service+Endpoints registered in ${BUILDS_NS}: gitea → $gitea_k3d_ip"
}

# ─── Phase 2: Jenkins ─────────────────────────────────────────────────────────

step "Jenkins"

info "Building Jenkins image (plugin downloads ~5-10 min on first run)..."
Invoke-Dc build jenkins

Invoke-Dc up -d jenkins

Wait-Http "http://localhost:8080/login" "Jenkins HTTP" 60 5

info "Waiting for Jenkins to finish initialising (JCasC)..."
$crumbJson = ""
for ($attempt = 1; $attempt -le 30; $attempt++) {
    $crumbJson = & curl.exe -sf -u "$($env:JENKINS_ADMIN_USER):$($env:JENKINS_ADMIN_PASSWORD)" `
        "http://localhost:8080/crumbIssuer/api/json" 2>$null
    if ($crumbJson) { ok "Jenkins: fully initialised"; break }
    Write-Host -NoNewline "."
    Start-Sleep 6
}
Write-Host ""

if (-not $crumbJson) {
    warn "Jenkins did not initialise within 3 min. Showing container logs:"
    & docker compose -f "$SCRIPT_DIR\docker-compose.yml" logs --tail=40 jenkins 2>$null
    die "Jenkins not ready. Fix the issue above and re-run."
}

info "Generating Jenkins API token..."
$JENKINS_JAR  = "$env:TEMP\ap3_jenkins_jar"
$TOKEN_URL    = "http://localhost:8080/user/$($env:JENKINS_ADMIN_USER)/descriptorByName/jenkins.security.ApiTokenProperty/generateNewToken"
$JENKINS_TOKEN = ""

function Invoke-JenkinsGenerateToken {
    Remove-Item $JENKINS_JAR -Force -ErrorAction SilentlyContinue
    $crumbRaw = & curl.exe -sf --max-time 15 `
        -c $JENKINS_JAR -b $JENKINS_JAR `
        -u "$($env:JENKINS_ADMIN_USER):$($env:JENKINS_ADMIN_PASSWORD)" `
        "http://localhost:8080/crumbIssuer/api/json" 2>$null
    if (-not $crumbRaw) { return $false }

    $cf = $crumbRaw | & python3 -c "import sys,json; d=json.load(sys.stdin); print(d['crumbRequestField'])" 2>$null
    $cv = $crumbRaw | & python3 -c "import sys,json; d=json.load(sys.stdin); print(d['crumb'])"             2>$null
    if (-not $cf -or -not $cv) { return $false }

    info "  crumb field=$cf value=$($cv.Substring(0, [Math]::Min(12, $cv.Length)))..."

    $trRaw = & curl.exe -s --max-time 15 `
        -c $JENKINS_JAR -b $JENKINS_JAR `
        -u "$($env:JENKINS_ADMIN_USER):$($env:JENKINS_ADMIN_PASSWORD)" `
        -H "${cf}: ${cv}" `
        -H "Content-Type: application/x-www-form-urlencoded" `
        -X POST $TOKEN_URL `
        --data-urlencode "newTokenName=ap3-cli-token" 2>$null
    info "  token gen response: $($trRaw.Substring(0, [Math]::Min(120, $trRaw.Length)))"

    $tok = $trRaw | & python3 -c "import sys,json; print(json.load(sys.stdin)['data']['tokenValue'])" 2>$null
    if (-not $tok) { return $false }
    $script:JENKINS_TOKEN = $tok
    return $true
}

for ($attempt = 1; $attempt -le 3; $attempt++) {
    if (Invoke-JenkinsGenerateToken) { break }
    info "  Jenkins not ready for token gen — waiting for post-JCasC restart..."
    Start-Sleep 15
    Wait-Http "http://localhost:8080/login" "Jenkins (post-restart)" 30 5
    for ($i = 1; $i -le 20; $i++) {
        & curl.exe -sf --max-time 5 `
            -u "$($env:JENKINS_ADMIN_USER):$($env:JENKINS_ADMIN_PASSWORD)" `
            "http://localhost:8080/crumbIssuer/api/json" 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { break }
        Write-Host -NoNewline "."; Start-Sleep 5
    }
    Write-Host ""
}

if ($JENKINS_TOKEN) {
    Update-EnvFile $ENV_FILE "JENKINS_USER"  $env:JENKINS_ADMIN_USER
    Update-EnvFile $ENV_FILE "JENKINS_TOKEN" $JENKINS_TOKEN
    $env:JENKINS_TOKEN = $JENKINS_TOKEN
    ok "Jenkins API token generated"
} else {
    warn "Could not auto-generate Jenkins API token."
    warn "Do it manually: Jenkins → $($env:JENKINS_ADMIN_USER) → Configure → API Token → Add new token"
}

Read-EnvFile $ENV_FILE

# ─── Generate bootstrap-config.yaml ──────────────────────────────────────────

step "Generating testenv/bootstrap-config.yaml"

@"
# bootstrap-config.yaml — Pre-answered wizard config for the AP3 testenv.
# Generated by testenv/create.ps1 on $timestamp.
# Usage:
#   Get-Content testenv\.env | ForEach-Object { `$k,`$v = `$_ -split '=',2; Set-Item "Env:`$k" `$v }
#   python bootstrap/bootstrap.py --config testenv/bootstrap-config.yaml

# ── Platform instance location ───────────────────────────────────────────────
platform_target_dir: "../platform-instance"

# ── Git hosting (Gitea) ──────────────────────────────────────────────────────
github_url: "http://localhost:3000"
github_api_path: "api/v1"
github_account_type: "org"
github_org: "ap3"
platform_repo_name: "platform"
shared_lib_repo_name: "jenkins-shared-lib"

# ── Jenkins ──────────────────────────────────────────────────────────────────
jenkins_url: "http://localhost:8080"
# Jenkins container reaches Gitea via the Docker service hostname, not localhost.
jenkins_git_url: "http://gitea:3000"
# Gitea container reaches Jenkins via the Docker service hostname for webhook delivery.
jenkins_hook_url: "http://jenkins:8080"

# ── Platform defaults ────────────────────────────────────────────────────────
platform: "openshift"
cluster_prefix: "openshift"

# ── Standard environments — all mapped to the local k3d cluster ──────────────
environments:
  prod:
    name: "prod"
    cluster: "openshift-prod"
    api_url: "$($env:K8S_API_URL)"
    context: "k3d-ap3"
    registry: "localhost:5000"
    namespace: "platform-prod"
  val:
    name: "val"
    cluster: "openshift-val"
    api_url: "$($env:K8S_API_URL)"
    context: "k3d-ap3"
    registry: "localhost:5000"
    namespace: "platform-val"
  dev:
    name: "dev"
    cluster: "openshift-dev"
    api_url: "$($env:K8S_API_URL)"
    context: "k3d-ap3"
    registry: "localhost:5000"
    namespace: "platform-dev"
"@ | Set-Content -Path "$SCRIPT_DIR\bootstrap-config.yaml" -Encoding UTF8

ok "testenv/bootstrap-config.yaml written  (K8S_API_URL=$($env:K8S_API_URL))"

# ─── Service health checks ────────────────────────────────────────────────────

step "Service health checks"

function Test-Svc([string]$Label, [string]$Url, [string]$Pattern = "") {
    $out = & curl.exe -sf --max-time 5 $Url 2>$null
    if ($LASTEXITCODE -eq 0 -and (-not $Pattern -or $out -match $Pattern)) {
        ok "$Label  →  $Url"
    } else {
        warn "$Label  NOT reachable at $Url"
        info "  Check: curl -v $Url"
    }
}

Test-Svc "Gitea"     "http://localhost:3000"                   "Gitea"
Test-Svc "SonarQube" "http://localhost:9000/api/system/status" '"status":"UP"'
Test-Svc "Registry"  "http://localhost:5000/v2/"               ""
Test-Svc "Jenkins"   "http://localhost:8080/login"             "Jenkins"

& kubectl cluster-info --context "k3d-$CLUSTER_NAME" 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    ok "k3d API  →  https://localhost:${K3D_API_PORT}  (kubectl OK)"
} else {
    warn "k3d API not responding — check: kubectl cluster-info --context k3d-$CLUSTER_NAME"
}

# ─── Summary ──────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "╔══════════════════════════════════════════════════════════════════════╗"
Write-Host "║          AP3 Local Test Environment — READY                         ║"
Write-Host "╠══════════════════════════════════════════════════════════════════════╣"
Write-Host "║                                                                      ║"
Write-Host ("║  {0,-24} {1,-45} ║" -f "Gitea (Git hosting)",   "http://localhost:3000")
Write-Host ("║  {0,-24} {1,-45} ║" -f "  Admin user",          "admin")
Write-Host ("║  {0,-24} {1,-45} ║" -f "  Admin password",      "see testenv\.users")
Write-Host ("║  {0,-24} {1,-45} ║" -f "  API token",           ($env:GITEA_TOKEN ?? "see .env"))
Write-Host ("║  {0,-24} {1,-45} ║" -f "  Org",                 "http://localhost:3000/ap3")
Write-Host "║                                                                      ║"
Write-Host ("║  {0,-24} {1,-45} ║" -f "Jenkins (CI/CD)",       "http://localhost:8080")
Write-Host ("║  {0,-24} {1,-45} ║" -f "  Admin user",          $env:JENKINS_ADMIN_USER)
Write-Host ("║  {0,-24} {1,-45} ║" -f "  Admin password",      "see testenv\.users")
Write-Host ("║  {0,-24} {1,-45} ║" -f "  API token",           ($env:JENKINS_TOKEN ?? "not generated — see above"))
Write-Host "║                                                                      ║"
Write-Host ("║  {0,-24} {1,-45} ║" -f "SonarQube (quality)",   "http://localhost:9000")
Write-Host ("║  {0,-24} {1,-45} ║" -f "  Admin user",          "admin")
Write-Host ("║  {0,-24} {1,-45} ║" -f "  Admin password",      "see testenv\.users")
Write-Host ("║  {0,-24} {1,-45} ║" -f "  Analysis token",      ($env:SONARQUBE_TOKEN ?? "see .env"))
Write-Host "║                                                                      ║"
Write-Host ("║  {0,-24} {1,-45} ║" -f "Registry",              "localhost:5000  (no auth in test env)")
Write-Host ("║  {0,-24} {1,-45} ║" -f "k3d API",               "https://localhost:${K3D_API_PORT}")
Write-Host "║                                                                      ║"
Write-Host "╚══════════════════════════════════════════════════════════════════════╝"
Write-Host ""
Write-Host "  API tokens → testenv\.env   (load for CLI/bootstrap usage)"
Write-Host "  Passwords  → testenv\.users (for manual web UI access)"
Write-Host ""
Write-Host "NEXT STEPS:"
Write-Host "  1. Activate credentials:"
Write-Host "       Get-Content testenv\.env | ForEach-Object { `$k,`$v = `$_ -split '=',2; Set-Item `"Env:`$k`" `$v }"
Write-Host "  2. Bootstrap the platform:"
Write-Host "       python bootstrap\bootstrap.py --config testenv\bootstrap-config.yaml"
Write-Host "  3. Test: create a service and watch Jenkins pick it up:"
Write-Host "       cd ..\platform-instance; python platform\scripts\platform_cli.py service create --name my-test-svc --owner me --template springboot"
Write-Host "       kubectl -n jenkins-builds get pods -w"
Write-Host "  4. Browse Gitea:  http://localhost:3000/ap3"
