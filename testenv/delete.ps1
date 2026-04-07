# delete.ps1 — Completely remove the AP3 test environment (Windows / PowerShell)
#
# WARNING: DESTRUCTIVE — removes all containers, volumes, the k3d cluster,
# the Docker network, and testenv\.env. This cannot be undone.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File testenv\delete.ps1 [-Force]

#Requires -Version 5.1

param(
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$SCRIPT_DIR   = $PSScriptRoot
$ENV_FILE     = "$SCRIPT_DIR\.env"
$CLUSTER_NAME = "ap3"

function info($msg) { Write-Host "[INFO]  $msg" -ForegroundColor Cyan }
function ok($msg)   { Write-Host "[ OK ]  $msg" -ForegroundColor Green }
function warn($msg) { Write-Host "[WARN]  $msg" -ForegroundColor Yellow }

function Invoke-Dc {
    $dcArgs = @("-f", "$SCRIPT_DIR\docker-compose.yml")
    if (Test-Path $ENV_FILE) {
        $dcArgs = @("--env-file", $ENV_FILE) + $dcArgs
    }
    & docker compose @dcArgs @args
}

# ── Confirm unless -Force ─────────────────────────────────────────────────────

if (-not $Force) {
    Write-Host ""
    warn "This will PERMANENTLY DELETE:"
    Write-Host "   * All containers  (Jenkins, SonarQube, PostgreSQL, Registry, Gitea)"
    Write-Host "   * All volumes     (Jenkins config, SonarQube data, DB data, Gitea data)"
    Write-Host "   * k3d cluster '$CLUSTER_NAME' and all workloads"
    Write-Host "   * $ENV_FILE"
    Write-Host ""
    $confirm = Read-Host "Type 'yes' to confirm"
    if ($confirm -ne "yes") {
        Write-Host "Aborted."
        exit 0
    }
}

# ── Compose down (remove containers + volumes) ────────────────────────────────

info "Removing compose services and volumes..."
try {
    Invoke-Dc down -v --remove-orphans
} catch {
    warn "docker compose down had errors (continuing)"
}

# Explicitly remove named volumes in case compose missed them
foreach ($vol in @(
    "testenv_postgres_data", "testenv_sonarqube_data", "testenv_sonarqube_extensions",
    "testenv_sonarqube_logs", "testenv_registry_data", "testenv_gitea_data",
    "testenv_jenkins_data"
)) {
    & docker volume rm $vol 2>$null
    if ($LASTEXITCODE -eq 0) { info "  removed volume $vol" }
}
ok "Compose cleaned up"

# ── k3d cluster ───────────────────────────────────────────────────────────────

info "Deleting k3d cluster '$CLUSTER_NAME'..."
& k3d cluster delete $CLUSTER_NAME 2>$null
if ($LASTEXITCODE -eq 0) {
    ok "k3d cluster deleted"
} else {
    warn "k3d cluster not found or already deleted"
}

# ── Jenkins image ──────────────────────────────────────────────────────────────

info "Removing Jenkins image ap3-jenkins:local..."
& docker rmi ap3-jenkins:local 2>$null
if ($LASTEXITCODE -eq 0) {
    ok "Image removed"
} else {
    warn "Image not found or already removed"
}

# ── .env file ──────────────────────────────────────────────────────────────────

if (Test-Path $ENV_FILE) {
    $backup = "$ENV_FILE.deleted.$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())"
    Copy-Item $ENV_FILE $backup -ErrorAction SilentlyContinue
    Remove-Item $ENV_FILE -Force
    ok ".env removed (backup kept as $(Split-Path $backup -Leaf))"
}

# ── Restore platform.yaml backup if present ───────────────────────────────────

$platformBak = Join-Path (Split-Path $SCRIPT_DIR -Parent) "platform.yaml.bak"
if (Test-Path $platformBak) {
    info "Restoring platform.yaml from backup..."
    Copy-Item $platformBak (Join-Path (Split-Path $SCRIPT_DIR -Parent) "platform.yaml") -Force
    ok "platform.yaml restored"
}

Write-Host ""
ok "Test environment fully removed."
Write-Host "Re-create with:  powershell -ExecutionPolicy Bypass -File testenv\create.ps1"
