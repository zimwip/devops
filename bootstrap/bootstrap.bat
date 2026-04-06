@echo off
:: bootstrap.bat — First-time AP3 platform setup (Windows CMD)
:: Idempotent: checks root commit marker before running.
::
:: Usage:
::   bootstrap.bat              interactive wizard
::   bootstrap.bat --yes        non-interactive (CI)
::   bootstrap.bat --force      bypass already-bootstrapped check
::
:: To fully reset:  rmdir /s /q .git  &&  bootstrap.bat

setlocal enabledelayedexpansion
set SCRIPT_DIR=%~dp0
set YES_FLAG=
set FORCE=0
set BOOTSTRAP_MARKER=chore: initial AP3 platform bootstrap

for %%A in (%*) do (
    if "%%A"=="--yes"   set YES_FLAG=--yes
    if "%%A"=="-y"      set YES_FLAG=--yes
    if "%%A"=="--force" set FORCE=1
)

echo.
echo   AP3 Platform Bootstrap (Windows CMD)
echo   ------------------------------------------
echo   Repo: %SCRIPT_DIR%
echo.

:: ── Already-bootstrapped check ─────────────────────────────────────────────
:: Read the root commit subject (--max-parents=0 = commits with no parent).
:: If it matches the bootstrap marker the platform is already set up.
if "!FORCE!"=="0" (
    for /f "delims=" %%S in ('git log --max-parents=0 --format="%%s" 2^>nul') do set ROOT_SUBJECT=%%S
    if "!ROOT_SUBJECT!"=="!BOOTSTRAP_MARKER!" (
        echo   This repository has already been bootstrapped.
        echo.
        for /f "delims=" %%L in ('git log --max-parents=0 --format="%%h %%ci" 2^>nul') do echo   Root commit: %%L
        echo.
        echo   To re-run the wizard:     python scripts\wizard.py
        echo   To add a cluster:         platform.bat cluster add ...
        echo   To seed demo data:        demo.bat
        echo   To fully reset:           rmdir /s /q .git  ^&^&  bootstrap.bat
        echo   To bypass this check:     bootstrap.bat --force
        echo.
        pause & exit /b 0
    )
)

:: ── Python ──────────────────────────────────────────────────────────────────
echo   -^> Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo   [error] Python not found. Install from https://python.org
    pause & exit /b 1
)
python --version

echo   -^> Installing Python dependencies...
python -m pip install -r "%SCRIPT_DIR%scripts\requirements.txt" --quiet
if errorlevel 1 ( echo   [error] pip install failed & pause & exit /b 1 )
echo   OK Python dependencies installed

:: ── Node ────────────────────────────────────────────────────────────────────
node --version >nul 2>&1
if errorlevel 1 (
    echo   ! Node.js not found - skipping frontend setup
    goto :git_init
)
echo   -^> Installing Node dependencies...
cd /d "%SCRIPT_DIR%dashboard\frontend"
call npm install --silent
if errorlevel 1 ( echo   ! npm install failed )
cd /d "%SCRIPT_DIR%"
echo   OK Node dependencies installed

:: ── Git init ────────────────────────────────────────────────────────────────
:git_init
echo.
echo   -^> Checking git repository...
git rev-parse --git-dir >nul 2>&1
if errorlevel 1 (
    echo   -^> Initialising git repository...
    git init -b main
    git config user.email "platform-bootstrap@ap3.local"
    git config user.name "AP3 Bootstrap"
    echo   OK Git repository initialised
) else (
    echo   OK Git repository already exists
)

:: ── Wizard ──────────────────────────────────────────────────────────────────
echo.
echo   -^> Running environment setup wizard...
python "%SCRIPT_DIR%scripts\wizard.py" !YES_FLAG!
if errorlevel 1 ( echo   [error] Wizard failed & pause & exit /b 1 )

:: ── Initial git commit (bootstrap marker) ───────────────────────────────────
echo.
echo   -^> Creating initial platform commit...
git add --all >nul 2>&1
git diff --cached --quiet >nul 2>&1
if errorlevel 1 (
    git commit -m "chore: initial AP3 platform bootstrap" >nul 2>&1
    echo   OK Initial commit created -- bootstrap marker set
) else (
    echo   OK Nothing new to commit
)

:: ── Summary ─────────────────────────────────────────────────────────────────
echo.
echo   ------------------------------------------
echo   Optional environment variables:
echo.
echo     set GITHUB_TOKEN=ghp_...
echo     set JENKINS_USER=admin
echo     set JENKINS_TOKEN=...
echo.
echo   Quick start:
echo     platform.bat dev          Start API + dashboard
echo     platform.bat env list     List environments
echo     platform.bat help         Show all commands
echo.
echo   Optional - populate with demo data:
echo     demo.bat
echo.
pause
