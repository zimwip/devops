@echo off
:: bootstrap/delete.bat — Remove an AP3 platform instance from all tooling (Windows CMD)
::
:: Mirrors bootstrap/delete.sh — delegates everything to delete.py.
::
:: Usage:
::   bootstrap\delete.bat                         uses .bootstrap-state.yaml
::   bootstrap\delete.bat --config <yaml>         uses config file
::   bootstrap\delete.bat --keep-repos            skip GitHub/Gitea deletion
::   bootstrap\delete.bat --keep-jenkins          skip Jenkins job deletion
::   bootstrap\delete.bat --keep-jenkins-lib      skip Jenkins lib config removal
::   bootstrap\delete.bat --keep-sonar            skip SonarQube deletion
::   bootstrap\delete.bat --keep-local            skip local directory removal
::   bootstrap\delete.bat --keep-platform-state   skip toolkit state reset
::   bootstrap\delete.bat --yes                   skip confirmation prompts
::
:: Required environment variables (for the operations you don't skip):
::   GITHUB_TOKEN       GitHub/Gitea API token
::   JENKINS_USER       Jenkins username
::   JENKINS_TOKEN      Jenkins API token
::   SONARQUBE_TOKEN    SonarQube user token (skipped if not set)

setlocal enabledelayedexpansion
set SCRIPT_DIR=%~dp0

echo.
echo   AP3 Platform Delete (Windows CMD)
echo   ------------------------------------------
echo.

:: ── Python ───────────────────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo   [error] Python not found. Install from https://python.org
    pause & exit /b 1
)

:: ── Run delete.py with all forwarded arguments ────────────────────────────────
python "%SCRIPT_DIR%scripts\delete.py" %*
if errorlevel 1 ( pause & exit /b 1 )
