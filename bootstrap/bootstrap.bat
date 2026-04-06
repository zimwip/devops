@echo off
:: bootstrap.bat — First-time AP3 platform setup (Windows CMD)
::
:: Usage:
::   bootstrap.bat              interactive wizard
::   bootstrap.bat --yes        non-interactive (CI)
::
:: To fully reset:  rmdir /s /q .git  &&  bootstrap.bat

setlocal enabledelayedexpansion
set SCRIPT_DIR=%~dp0
set YES_FLAG=
set BOOTSTRAP_MARKER=chore: initial AP3 platform bootstrap

for %%A in (%*) do (
    if "%%A"=="--yes"   set YES_FLAG=--yes
    if "%%A"=="-y"      set YES_FLAG=--yes
)

echo.
echo   AP3 Platform Bootstrap (Windows CMD)
echo   ------------------------------------------
echo   Repo: %SCRIPT_DIR%
echo.


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

:: ── Wizard ──────────────────────────────────────────────────────────────────
:git_init
echo.
echo   -^> Running environment setup wizard...
python "%SCRIPT_DIR%scripts\wizard.py" !YES_FLAG!
if errorlevel 1 ( echo   [error] Wizard failed & pause & exit /b 1 )

:: ── Initial git commit on the platform-instance ──────────────────────────────
:: Read the platform_target_dir written by wizard.py into .bootstrap-state.yaml
echo.
echo   -^> Creating initial platform commit...
for /f "usebackq delims=" %%T in (
    `python -c "import yaml; print(yaml.safe_load(open(r'%SCRIPT_DIR%.bootstrap-state.yaml'))['platform_target_dir'])"`
) do set PLATFORM_DIR=%%T

if not defined PLATFORM_DIR (
    echo   [error] Could not determine platform target directory & pause & exit /b 1
)

cd /d "!PLATFORM_DIR!"
git add --all >nul 2>&1
git diff --cached --quiet >nul 2>&1
if errorlevel 1 (
    git commit -m "chore: initial AP3 platform bootstrap" >nul 2>&1
    echo   OK Initial commit created in !PLATFORM_DIR!
) else (
    echo   OK Nothing new to commit
)

:: ── Node dependencies in platform-instance ───────────────────────────────────
node --version >nul 2>&1
if not errorlevel 1 (
    if exist "!PLATFORM_DIR!\dashboard\frontend" (
        echo   -^> Installing Node dependencies...
        cd /d "!PLATFORM_DIR!\dashboard\frontend"
        call npm install --silent
        echo   OK Node dependencies installed
    )
)
cd /d "%SCRIPT_DIR%"

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
