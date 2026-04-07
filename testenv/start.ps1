# start.ps1 — Start a previously created AP3 test environment (Windows / PowerShell)
#
# Use this after a reboot or after running stop.ps1.
# Does NOT re-run setup — all credentials remain as they were.
#
# Requirements:
#   - Docker Desktop (with WSL2 backend)
#   - k3d   (https://k3d.io)
#   - kubectl
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File testenv\start.ps1

#Requires -Version 5.1

$ErrorActionPreference = 'Stop'
$SCRIPT_DIR   = $PSScriptRoot
$ENV_FILE     = "$SCRIPT_DIR\.env"
$CLUSTER_NAME = "ap3"

function info($msg)  { Write-Host "[INFO]  $msg" -ForegroundColor Cyan }
function ok($msg)    { Write-Host "[ OK ]  $msg" -ForegroundColor Green }
function warn($msg)  { Write-Host "[WARN]  $msg" -ForegroundColor Yellow }
function die($msg)   { Write-Host "[ERR ]  $msg" -ForegroundColor Red; exit 1 }

function Read-EnvFile([string]$Path) {
    if (-not (Test-Path $Path)) { return }
    Get-Content $Path | Where-Object { $_ -match '^[^#\s]\w*=' } | ForEach-Object {
        $idx = $_.IndexOf('=')
        $k   = $_.Substring(0, $idx).Trim()
        $v   = $_.Substring($idx + 1).Trim()
        Set-Item -Path "Env:$k" -Value $v
    }
}

function Invoke-Dc {
    docker compose --env-file $ENV_FILE -f "$SCRIPT_DIR\docker-compose.yml" @args
}

# ── Guards ────────────────────────────────────────────────────────────────────

if (-not (Test-Path $ENV_FILE)) {
    die ".env not found — run create.ps1 first"
}
Read-EnvFile $ENV_FILE

# ── WSL2 vm.max_map_count (SonarQube / Elasticsearch) ────────────────────────

try {
    $currentMap = [int](wsl -d docker-desktop -- sysctl -n vm.max_map_count 2>$null)
    if ($currentMap -lt 524288) {
        info "Applying vm.max_map_count=524288 for SonarQube (WSL2)..."
        wsl -d docker-desktop -- sysctl -w vm.max_map_count=524288 2>$null
        ok "vm.max_map_count set"
    }
} catch {
    warn "Could not set vm.max_map_count via WSL2 (non-fatal — SonarQube may fail to start)"
}

# ── k3d ──────────────────────────────────────────────────────────────────────

info "Starting k3d cluster '$CLUSTER_NAME'..."
$clusterList = & k3d cluster list 2>$null
if ($clusterList -match "(?m)^$CLUSTER_NAME\s") {
    & k3d cluster start $CLUSTER_NAME
    & k3d kubeconfig merge $CLUSTER_NAME --kubeconfig-switch-context
    ok "k3d cluster started"
} else {
    die "k3d cluster '$CLUSTER_NAME' not found — run create.ps1 first"
}

# ── Compose services ──────────────────────────────────────────────────────────

info "Starting compose services..."
Invoke-Dc up -d

# ── Re-register Gitea DNS in k3d ─────────────────────────────────────────────
# The gitea container gets a fresh IP on the k3d network after every restart.
# Update the headless Service+Endpoints in jenkins-builds so agent pods
# resolve http://gitea:3000 immediately via kube-dns.
info "Updating Gitea entry in k3d CoreDNS..."

& docker network connect "k3d-$CLUSTER_NAME" gitea 2>$null
if ($LASTEXITCODE -eq 0) { ok "Gitea connected to k3d-$CLUSTER_NAME" }
else                      { ok "Gitea already on k3d-$CLUSTER_NAME network" }

$inspectJson  = & docker network inspect "k3d-$CLUSTER_NAME" 2>$null
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

$BUILDS_NS = "jenkins-builds"

if (-not $gitea_k3d_ip) {
    warn "Could not determine Gitea IP — DNS not updated."
    warn "Jenkins builds may fail to clone repos. Re-run: testenv\start.ps1"
} else {
    # Update the headless Service+Endpoints so agent pods immediately resolve
    # the fresh container IP via kube-dns (no ConfigMap file-sync latency).
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
    ok "gitea Service+Endpoints updated in ${BUILDS_NS}: gitea → $gitea_k3d_ip"
}

ok "All services starting. Check status with:"
Write-Host "   docker compose -f testenv\docker-compose.yml ps"
Write-Host "   kubectl cluster-info"
Write-Host ""
Write-Host ("   Jenkins:   http://localhost:8080  ({0} / see testenv\.users)" -f ($env:JENKINS_USER ?? "admin"))
Write-Host "   SonarQube: http://localhost:9000  (admin / see testenv\.users)"
