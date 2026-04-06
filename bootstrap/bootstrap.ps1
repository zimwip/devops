# bootstrap.ps1 — First-time AP3 platform setup (PowerShell)
# Idempotent: checks root commit marker before running.
#
# Usage:
#   Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
#   .\bootstrap.ps1              # interactive wizard
#   .\bootstrap.ps1 -yes         # non-interactive (CI)
#   .\bootstrap.ps1 -force       # bypass already-bootstrapped check
#
# To fully reset:  Remove-Item -Recurse -Force .git; .\bootstrap.ps1

param(
    [switch]$yes,
    [switch]$force
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$BootstrapMarker = "chore: initial AP3 platform bootstrap"

function Write-Step($msg) { Write-Host "  -> $msg" -ForegroundColor Cyan }
function Write-OK($msg)   { Write-Host "  OK $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  !  $msg" -ForegroundColor Yellow }

Write-Host ""
Write-Host "  AP3 Platform Bootstrap (PowerShell)" -ForegroundColor White
Write-Host "  ------------------------------------------"
Write-Host "  Repo: $Root"
Write-Host ""

# ── Already-bootstrapped check ────────────────────────────────────────────────
# Read the root commit subject — the commit with no parents.
# If it matches the bootstrap marker, setup was already completed.
if (-not $force) {
    $rootSubject = (git log --max-parents=0 --format="%s" 2>$null) -join "" 
    if ($rootSubject -eq $BootstrapMarker) {
        Write-Host "  This repository has already been bootstrapped." -ForegroundColor Yellow
        Write-Host ""
        $rootInfo = (git log --max-parents=0 --format="%h %ci" 2>$null) -join ""
        Write-Host "  Root commit: $rootInfo"
        Write-Host ""
        Write-Host "  To re-run the wizard:    python scripts\wizard.py"
        Write-Host "  To add a cluster:        .\platform.ps1 cluster add ..."
        Write-Host "  To seed demo data:       .\demo.ps1"
        Write-Host "  To fully reset:          Remove-Item -Recurse -Force .git; .\bootstrap.ps1"
        Write-Host "  To bypass this check:    .\bootstrap.ps1 -force"
        Write-Host ""
        exit 0
    }
}

# ── Python ────────────────────────────────────────────────────────────────────
Write-Step "Checking Python"
try {
    $pv = (python --version 2>&1).ToString()
    Write-Host "  Found: $pv"
} catch {
    Write-Host "  [error] Python not found. Install from https://python.org" -ForegroundColor Red
    exit 1
}

Write-Step "Installing Python dependencies"
pip install -r "$Root\scripts\requirements.txt" --quiet
Write-OK "Python dependencies installed"

# ── Node ──────────────────────────────────────────────────────────────────────
Write-Step "Checking Node.js"
try {
    $nv = (node --version 2>&1).ToString()
    Write-Host "  Found: $nv"
    Write-Step "Installing Node dependencies"
    Push-Location "$Root\dashboard\frontend"
    npm install --silent
    Pop-Location
    Write-OK "Node dependencies installed"
} catch {
    Write-Warn "Node.js not found — skipping (install from https://nodejs.org)"
}

# ── Git init ──────────────────────────────────────────────────────────────────
Write-Step "Checking git repository"
try {
    git rev-parse --git-dir 2>$null | Out-Null
    Write-OK "Git repository already exists"
} catch {
    Write-Step "Initialising git repository"
    git init -b main
    git config user.email "platform-bootstrap@ap3.local"
    git config user.name "AP3 Bootstrap"
    Write-OK "Git repository initialised (branch: main)"
}

# Ensure git identity
if (-not (git config user.email 2>$null)) {
    git config user.email "platform-bootstrap@ap3.local"
    git config user.name  "AP3 Bootstrap"
}

# ── Wizard ────────────────────────────────────────────────────────────────────
Write-Step "Running environment setup wizard"
$wizArgs = @()
if ($yes) { $wizArgs += "--yes" }
python "$Root\scripts\wizard.py" @wizArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host "  [error] Wizard failed" -ForegroundColor Red; exit 1
}

# ── Initial git commit (bootstrap marker) ─────────────────────────────────────
Write-Step "Creating initial platform commit (bootstrap marker)"
git add --all 2>$null | Out-Null
$staged = (git diff --cached --name-only 2>$null)
if ($staged) {
    git commit -m $BootstrapMarker 2>$null | Out-Null
    Write-OK "Initial commit created — bootstrap marker set"
} else {
    Write-OK "Nothing new to commit"
}

# ── Summary ───────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  ------------------------------------------"
Write-Host '  Optional: $env:GITHUB_TOKEN="ghp_..."'
Write-Host '            $env:JENKINS_USER="admin"'
Write-Host '            $env:JENKINS_TOKEN="..."'
Write-Host ""
Write-Host "  Quick start:"
Write-Host "    .\platform.ps1 dev         Start API + dashboard"
Write-Host "    .\platform.ps1 env list    List environments"
Write-Host "    .\platform.ps1 help"
Write-Host ""
Write-Host "  Optional — seed demo data:"
Write-Host "    .\demo.ps1"
Write-Host ""
