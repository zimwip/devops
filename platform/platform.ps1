# platform.ps1 — PowerShell launcher (richer alternative to platform.bat)
# Supports the same commands as platform.bat with better output formatting.
#
# Usage:
#   .\platform.ps1 help
#   .\platform.ps1 dev
#   .\platform.ps1 env list
#   etc.

param(
    [Parameter(Position=0)] [string]$Command = "help",
    [Parameter(Position=1)] [string]$Sub = "",
    [Parameter(Position=2)] [string]$Arg1 = "",
    [Parameter(Position=3)] [string]$Arg2 = "",
    [Parameter(Position=4)] [string]$Arg3 = ""
)

$ErrorActionPreference = "Stop"
$Root    = Split-Path -Parent $MyInvocation.MyCommand.Path
$CLI     = Join-Path $Root "scripts\platform_cli.py"
$ApiPort = 5173
$UiPort  = 5174

function Write-Step($msg)    { Write-Host "  -> $msg" -ForegroundColor Cyan }
function Write-OK($msg)      { Write-Host "  OK $msg" -ForegroundColor Green }
function Write-Warn($msg)    { Write-Host "  !  $msg" -ForegroundColor Yellow }
function Write-Err($msg)     { Write-Host "  [error] $msg" -ForegroundColor Red; exit 1 }
function Run-CLI([string[]]$Args) { python $CLI @Args }

switch ($Command) {

    "help" {
        Write-Host ""
        Write-Host "  Platform — PowerShell launcher" -ForegroundColor White
        Write-Host "  ------------------------------------------------"
        Write-Host "  .\platform.ps1 install                  Install dependencies"
        Write-Host "  .\platform.ps1 dev                      Start API + UI"
        Write-Host "  .\platform.ps1 dev-api                  FastAPI only  -> :$ApiPort"
        Write-Host "  .\platform.ps1 dev-ui                   React only    -> :$UiPort"
        Write-Host "  .\platform.ps1 test                     Run pytest"
        Write-Host "  .\platform.ps1 build                    Build frontend"
        Write-Host ""
        Write-Host "  Environments:"
        Write-Host "  .\platform.ps1 env list"
        Write-Host "  .\platform.ps1 env info    <name>"
        Write-Host "  .\platform.ps1 env create  <name> [base]"
        Write-Host "  .\platform.ps1 env destroy <name>"
        Write-Host "  .\platform.ps1 env diff    <from> <to>"
        Write-Host ""
        Write-Host "  Services:"
        Write-Host "  .\platform.ps1 svc list"
        Write-Host "  .\platform.ps1 svc info   <name>"
        Write-Host "  .\platform.ps1 svc create <name> <template> <owner>"
        Write-Host "    templates: springboot | react | python-api"
        Write-Host ""
        Write-Host "  For env create with cluster/platform, use the Python CLI directly:"
        Write-Host "  python scripts\platform_cli.py env create --name n --base staging --platform aws --cluster eks-dev"
        Write-Host ""
        Write-Host "  Deployments:"
        Write-Host "  .\platform.ps1 deploy <service> <version> <env> [--force]"
        Write-Host ""
        Write-Host "  POC shortcuts:"
        Write-Host "  .\platform.ps1 poc create  <name> [base]"
        Write-Host "  .\platform.ps1 poc destroy <name>"
        Write-Host ""
    }

    "install" {
        Write-Step "Installing Python dependencies"
        pip install -r "$Root\scripts\requirements.txt" --quiet
        Write-OK "Python deps installed"

        Write-Step "Installing Node dependencies"
        try {
            Push-Location "$Root\dashboard\frontend"
            npm install --silent
            Pop-Location
            Write-OK "Node deps installed"
        } catch {
            Write-Warn "Node.js not found - skipping (install from https://nodejs.org)"
        }
        Write-Host ""
        Write-OK "Ready. Run: .\platform.ps1 dev"
    }

    "dev" {
        Write-Step "Starting API (:$ApiPort) and UI (:$UiPort) in separate windows"
        $apiCmd = "cd '$Root\dashboard\backend'; `$env:PYTHONPATH='$Root\scripts;.'; uvicorn app:app --reload --port $ApiPort"
        $uiCmd  = "cd '$Root\dashboard\frontend'; npm run dev"
        Start-Process powershell -ArgumentList "-NoExit", "-Command", $apiCmd -WindowStyle Normal
        Start-Sleep -Seconds 2
        Start-Process powershell -ArgumentList "-NoExit", "-Command", $uiCmd  -WindowStyle Normal
        Write-OK "Servers started. Close the windows to stop."
        Write-Host "  API -> http://localhost:$ApiPort"
        Write-Host "  UI  -> http://localhost:$UiPort"
        Write-Host "  Docs-> http://localhost:$ApiPort/docs"
    }

    "dev-api" {
        Write-Step "Starting FastAPI on :$ApiPort"
        $env:PYTHONPATH = "$Root\scripts;$Root\dashboard\backend"
        Set-Location "$Root\dashboard\backend"
        uvicorn app:app --reload --port $ApiPort
    }

    "dev-ui" {
        Write-Step "Starting React dev server on :$UiPort"
        Set-Location "$Root\dashboard\frontend"
        npm run dev
    }

    "test" {
        Write-Step "Running pytest"
        $env:PYTHONPATH = "$Root\scripts;$Root\dashboard\backend"
        python -m pytest "$Root\dashboard\backend\tests\" -v --tb=short
    }

    "build" {
        Write-Step "Building React frontend"
        Push-Location "$Root\dashboard\frontend"
        npm run build
        Pop-Location
        Write-OK "Frontend built -> dashboard\frontend\dist\"
    }

    "env" {
        switch ($Sub) {
            "list"    { Run-CLI "env", "list" }
            "info"    { if (!$Arg1) { Write-Err "env info requires a name" }
                        Run-CLI "env", "info", "--name", $Arg1 }
            "create"  {
                        if (!$Arg1) { Write-Err "env create requires a name" }
                        $base = if ($Arg2) { $Arg2 } else { "staging" }
                        $cliArgs = @("env", "create", "--name", $Arg1, "--type", "poc", "--base", $base)
                        # Arg3=namespace, Arg4 would need extra param — use named flags directly
                        if ($Arg3) { $cliArgs += "--namespace", $Arg3 }
                        Run-CLI @cliArgs }
            "destroy" { if (!$Arg1) { Write-Err "env destroy requires a name" }
                        Run-CLI "env", "destroy", "--name", $Arg1 }
            "diff"    { if (!$Arg1 -or !$Arg2) { Write-Err "env diff requires two env names" }
                        Run-CLI "env", "diff", "--from", $Arg1, "--to", $Arg2 }
            default   { Write-Err "Unknown env subcommand: $Sub. Use: list|info|create|destroy|diff" }
        }
    }

    "svc" {
        switch ($Sub) {
            "list"   { Run-CLI "service", "list" }
            "info"   { if (!$Arg1) { Write-Err "svc info requires a name" }
                       Run-CLI "service", "info", "--name", $Arg1 }
            "create" {
                       if (!$Arg1 -or !$Arg2) {
                           Write-Err "Usage: svc create <n> <owner> [--template tpl | --fork-from svc | --external-repo url]"
                       }
                       # Arg1=name  Arg2=owner  Arg3=flag  Arg4=value
                       $cliArgs = @("service", "create", "--name", $Arg1, "--owner", $Arg2)
                       if ($Arg3) { $cliArgs += $Arg3 }
                       if ($Arg4) { $cliArgs += $Arg4 }
                       if (-not $Arg3) { $cliArgs += "--template", "springboot" }
                       Run-CLI @cliArgs }
            default  { Write-Err "Unknown svc subcommand: $Sub. Use: list|info|create" }
        }
    }

    "deploy" {
        if (!$Sub -or !$Arg1 -or !$Arg2) {
            Write-Err "Usage: .\platform.ps1 deploy <service> <version> <env> [--force]"
        }
        $cliArgs = @("deploy", "--service", $Sub, "--version", $Arg1, "--env", $Arg2)
        if ($Arg3 -eq "--force") { $cliArgs += "--force" }
        Run-CLI @cliArgs
    }

    "poc" {
        switch ($Sub) {
            "create"  {
                        if (!$Arg1) { Write-Err "poc create requires a name" }
                        $base = if ($Arg2) { $Arg2 } else { "staging" }
                        $cliArgs = @("env", "create", "--name", $Arg1, "--type", "poc", "--base", $base)
                        if ($Arg3) { $cliArgs += "--namespace", $Arg3 }
                        Run-CLI @cliArgs }
            "destroy" { if (!$Arg1) { Write-Err "poc destroy requires a name" }
                        Run-CLI "env", "destroy", "--name", $Arg1 }
            default   { Write-Err "Unknown poc subcommand: $Sub. Use: create|destroy" }
        }
    }

    default {
        Write-Err "Unknown command '$Command'. Run: .\platform.ps1 help"
    }
}
