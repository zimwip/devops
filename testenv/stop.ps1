# stop.ps1 — Stop the AP3 test environment without deleting data (Windows / PowerShell)
#
# All data (Jenkins config, SonarQube projects, k8s resources) is preserved
# in Docker named volumes and the k3d cluster. Use start.ps1 to resume.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File testenv\stop.ps1

#Requires -Version 5.1

$ErrorActionPreference = 'Stop'
$SCRIPT_DIR   = $PSScriptRoot
$ENV_FILE     = "$SCRIPT_DIR\.env"
$CLUSTER_NAME = "ap3"

function info($msg) { Write-Host "[INFO]  $msg" -ForegroundColor Cyan }
function ok($msg)   { Write-Host "[ OK ]  $msg" -ForegroundColor Green }

function Invoke-Dc {
    $args2 = @("--env-file", $ENV_FILE, "-f", "$SCRIPT_DIR\docker-compose.yml") + $args
    & docker compose @args2
}

# ── Compose services ──────────────────────────────────────────────────────────

info "Stopping compose services..."
try {
    Invoke-Dc stop
    ok "Compose services stopped"
} catch {
    Write-Host "  (compose stop returned an error — continuing)" -ForegroundColor Yellow
}

# ── k3d ──────────────────────────────────────────────────────────────────────

info "Stopping k3d cluster '$CLUSTER_NAME'..."
& k3d cluster stop $CLUSTER_NAME 2>$null
if ($LASTEXITCODE -eq 0) {
    ok "k3d stopped"
} else {
    Write-Host "  (cluster not found or already stopped)"
}

Write-Host ""
Write-Host "Environment stopped. Data preserved in Docker volumes."
Write-Host "Resume with:  powershell -ExecutionPolicy Bypass -File testenv\start.ps1"
