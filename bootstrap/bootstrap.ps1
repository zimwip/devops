# bootstrap.ps1 — First-time AP3 platform setup (PowerShell)
#
# Usage:
#   Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
#   .\bootstrap.ps1              # interactive wizard
#   .\bootstrap.ps1 -yes         # non-interactive (CI)
#
# To fully reset:  Remove-Item -Recurse -Force .git; .\bootstrap.ps1

param(
    [switch]$yes
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

# ── Wizard ────────────────────────────────────────────────────────────────────
Write-Step "Running environment setup wizard"
$wizArgs = @()
if ($yes) { $wizArgs += "--yes" }
python "$Root\scripts\wizard.py" @wizArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host "  [error] Wizard failed" -ForegroundColor Red; exit 1
}

# ── Initial git commit on the platform-instance ───────────────────────────────
# Read platform_target_dir from the state file written by wizard.py
Write-Step "Creating initial platform commit"
$stateFile = "$Root\.bootstrap-state.yaml"
$platformDir = python -c "import yaml; print(yaml.safe_load(open(r'$stateFile'))['platform_target_dir'])"
if (-not $platformDir -or $LASTEXITCODE -ne 0) {
    Write-Host "  [error] Could not determine platform target directory" -ForegroundColor Red; exit 1
}

Push-Location $platformDir
git add --all 2>$null | Out-Null
$staged = (git diff --cached --name-only 2>$null)
if ($staged) {
    git commit -m $BootstrapMarker 2>$null | Out-Null
    Write-OK "Initial commit created in $platformDir"
} else {
    Write-OK "Nothing new to commit"
}
Pop-Location

# ── Push to origin ────────────────────────────────────────────────────────────
Push-Location $platformDir
$originUrl = (git remote get-url origin 2>$null)
if ($originUrl) {
    Write-Step "Pushing to origin"
    $pushOk = $false

    git push -u origin main 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $pushOk = $true
    } else {
        # Remote may have a stale commit from a previous bootstrap run.
        # Force-push is safe: this repo belongs to this bootstrap instance.
        Write-Warn "Normal push rejected — remote has diverged history (stale bootstrap?)."
        Write-Warn "Force-pushing local content to origin..."
        git push --force -u origin main 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { $pushOk = $true }
    }

    if ($pushOk) {
        Write-OK "Pushed to $originUrl"

        # Strip embedded credentials from the push URL for the clean clone URL
        $cloneUrl = python -c @"
from urllib.parse import urlparse, urlunparse
u = urlparse('$originUrl')
netloc = u.hostname + (':' + str(u.port) if u.port else '')
print(urlunparse(u._replace(netloc=netloc)))
"@
        Write-Step "Replacing initialised repo with a clean clone"
        $parentDir = Split-Path -Parent $platformDir
        $cloneName = Split-Path -Leaf $platformDir
        Pop-Location
        Remove-Item -Recurse -Force $platformDir
        git clone $cloneUrl (Join-Path $parentDir $cloneName)
        if ($LASTEXITCODE -eq 0) {
            $platformDir = Join-Path $parentDir $cloneName
            Write-OK "Platform instance cloned at $platformDir"
        } else {
            Write-Warn "Clone failed — push succeeded but clone manually:"
            Write-Warn "  git clone $cloneUrl $platformDir"
        }
    } else {
        Pop-Location
        Write-Warn "git push failed — local commit created successfully."
        Write-Warn "Push manually: cd $platformDir; git push --force -u origin main"
    }
} else {
    Pop-Location
}

# ── Node dependencies in platform-instance ────────────────────────────────────
try {
    node --version 2>&1 | Out-Null
    $frontendDir = Join-Path $platformDir "dashboard\frontend"
    if (Test-Path $frontendDir) {
        Write-Step "Installing Node dependencies"
        Push-Location $frontendDir
        npm install --silent
        Pop-Location
        Write-OK "Node dependencies installed"
    }
} catch {
    Write-Warn "Node.js not found — run 'npm install' in dashboard/frontend manually"
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
