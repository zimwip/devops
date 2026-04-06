@echo off
setlocal enabledelayedexpansion

:: platform.bat — Platform CLI launcher for Windows
:: Equivalent of the Makefile for Windows (CMD and PowerShell compatible)
::
:: Usage:
::   platform.bat help
::   platform.bat install
::   platform.bat dev
::   platform.bat dev-api
::   platform.bat dev-ui
::   platform.bat test
::   platform.bat build
::   platform.bat env list
::   platform.bat env create <name> [base]
::   platform.bat env destroy <name>
::   platform.bat env diff <from> <to>
::   platform.bat svc list
::   platform.bat svc create <name> <template> <owner>
::   platform.bat svc info <name>
::   platform.bat deploy <service> <version> <env>
::   platform.bat poc create <name> [base]
::   platform.bat poc destroy <name>

set SCRIPT_DIR=%~dp0
set PYTHON=python
set PIP=pip
set API_PORT=5173
set UI_PORT=5174
set CLI=%SCRIPT_DIR%scripts\platform_cli.py

if "%1"=="" goto :help
if "%1"=="help" goto :help
if "%1"=="install" goto :install
if "%1"=="dev" goto :dev
if "%1"=="dev-api" goto :dev_api
if "%1"=="dev-ui" goto :dev_ui
if "%1"=="test" goto :test
if "%1"=="build" goto :build
if "%1"=="env" goto :env
if "%1"=="svc" goto :svc
if "%1"=="deploy" goto :deploy
if "%1"=="poc" goto :poc

echo [error] Unknown command: %1
goto :help

:: ── Help ──────────────────────────────────────────────────────────────────────
:help
echo.
echo   Platform — Windows launcher
echo   ------------------------------------------------
echo   platform.bat help                     This help
echo   platform.bat install                  Install Python + Node dependencies
echo   platform.bat dev                      Start API :5173 + UI :5174
echo   platform.bat dev-api                  Start FastAPI backend only
echo   platform.bat dev-ui                   Start React frontend only
echo   platform.bat test                     Run pytest suite
echo   platform.bat build                    Build React frontend
echo.
echo   Environment management:
echo   platform.bat env list                 List all environments
echo   platform.bat env info ^<name^>          Show environment details
echo   platform.bat env diff ^<from^> ^<to^>     Version diff
echo   platform.bat env create ^<name^> [base] [ns] [cluster] [platform] [--force]  Create POC
echo   platform.bat env destroy ^<name^>       Destroy POC environment
echo.
echo   Service management:
echo   platform.bat svc list                 List all services
echo   platform.bat svc info ^<name^>          Service detail
echo   platform.bat svc create ^<n^> ^<owner^> [--template tpl]  Template mode
  echo   platform.bat svc create ^<n^> ^<owner^> --fork-from ^<src^>     Fork mode
  echo   platform.bat svc create ^<n^> ^<owner^> --external-repo ^<url^> External mode
echo.
echo   Deployments:
echo   platform.bat deploy ^<svc^> ^<ver^> ^<env^> Trigger deployment
echo.
echo   POC shortcuts:
echo   platform.bat poc create ^<name^> [base] [namespace]  Create POC (= env create)
echo   platform.bat poc destroy ^<name^>       Destroy POC (= env destroy)
echo.
goto :eof

:: ── Install ───────────────────────────────────────────────────────────────────
:install
echo.
echo   -^> Installing Python dependencies...
%PIP% install -r "%SCRIPT_DIR%scripts\requirements.txt" --quiet
if errorlevel 1 ( echo [error] pip install failed & exit /b 1 )
echo   OK Python dependencies installed
echo.
echo   -^> Installing Node dependencies...
cd /d "%SCRIPT_DIR%dashboard\frontend"
call npm install --silent
if errorlevel 1 ( echo [warn] npm install failed - Node.js may not be installed )
cd /d "%SCRIPT_DIR%"
echo   OK Node dependencies installed
echo.
echo   Ready. Run: platform.bat dev
goto :eof

:: ── Dev servers ───────────────────────────────────────────────────────────────
:dev
echo.
echo   Starting API ^(:5173^) and UI ^(:5174^) in separate windows...
echo   API -^> http://localhost:%API_PORT%
echo   UI  -^> http://localhost:%UI_PORT%
echo.
start "Platform API" cmd /k "cd /d %SCRIPT_DIR%dashboard\backend && set PYTHONPATH=%SCRIPT_DIR%scripts;. && uvicorn app:app --reload --port %API_PORT%"
timeout /t 2 /nobreak >nul
start "Platform UI" cmd /k "cd /d %SCRIPT_DIR%dashboard\frontend && npm run dev"
echo   Both servers started. Close the windows to stop them.
goto :eof

:dev_api
echo   -^> Starting FastAPI on :%API_PORT%
echo   Swagger UI: http://localhost:%API_PORT%/docs
cd /d "%SCRIPT_DIR%dashboard\backend"
set PYTHONPATH=%SCRIPT_DIR%scripts;.
uvicorn app:app --reload --port %API_PORT%
goto :eof

:dev_ui
echo   -^> Starting React dev server on :%UI_PORT%
cd /d "%SCRIPT_DIR%dashboard\frontend"
npm run dev
goto :eof

:: ── Test ──────────────────────────────────────────────────────────────────────
:test
echo   -^> Running backend tests...
set PYTHONPATH=%SCRIPT_DIR%scripts;%SCRIPT_DIR%dashboard\backend
%PYTHON% -m pytest "%SCRIPT_DIR%dashboard\backend\tests\" -v --tb=short
goto :eof

:: ── Build ─────────────────────────────────────────────────────────────────────
:build
echo   -^> Building React frontend...
cd /d "%SCRIPT_DIR%dashboard\frontend"
call npm run build
cd /d "%SCRIPT_DIR%"
echo   OK Frontend built -^> dashboard\frontend\dist\
goto :eof

:: ── Env commands ──────────────────────────────────────────────────────────────
:env
if "%2"=="" goto :env_help
if "%2"=="list" goto :env_list
if "%2"=="info" goto :env_info
if "%2"=="create" goto :env_create
if "%2"=="destroy" goto :env_destroy
if "%2"=="diff" goto :env_diff
echo [error] Unknown env subcommand: %2
goto :env_help

:env_help
echo   Usage:
echo     platform.bat env list
echo     platform.bat env info ^<name^>
echo     platform.bat env create ^<name^> [base] [ns] [cluster] [platform]
echo     platform.bat env destroy ^<name^>
echo     platform.bat env diff ^<from^> ^<to^>
goto :eof

:env_list
%PYTHON% "%CLI%" env list
goto :eof

:env_info
if "%3"=="" ( echo [error] env info requires a name & goto :eof )
%PYTHON% "%CLI%" env info --name %3
goto :eof

:env_create
if "%3"=="" ( echo [error] env create requires a name & goto :eof )
set POC_BASE=staging
set POC_NS=
set POC_CLUSTER=
set POC_PLATFORM=
if not "%4"=="" set POC_BASE=%4
if not "%5"=="" set POC_NS=--namespace %5
if not "%6"=="" set POC_CLUSTER=--cluster %6
if not "%7"=="" set POC_PLATFORM=--platform %7
%PYTHON% "%CLI%" env create --name %3 --type poc --base %POC_BASE% %POC_NS% %POC_CLUSTER% %POC_PLATFORM%
goto :eof

:env_destroy
if "%3"=="" ( echo [error] env destroy requires a name & goto :eof )
%PYTHON% "%CLI%" env destroy --name %3
goto :eof

:env_diff
if "%3"=="" ( echo [error] env diff requires two env names & goto :eof )
if "%4"=="" ( echo [error] env diff requires two env names & goto :eof )
%PYTHON% "%CLI%" env diff --from %3 --to %4
goto :eof

:: ── Svc commands ──────────────────────────────────────────────────────────────
:svc
if "%2"=="" goto :svc_help
if "%2"=="list" goto :svc_list
if "%2"=="info" goto :svc_info
if "%2"=="create" goto :svc_create
echo [error] Unknown svc subcommand: %2
goto :svc_help

:svc_help
echo   Usage:
echo     platform.bat svc list
echo     platform.bat svc info ^<name^>
echo     platform.bat svc create ^<name^> ^<template^> ^<owner^>
echo       templates: springboot, react, python-api
goto :eof

:svc_list
%PYTHON% "%CLI%" service list
goto :eof

:svc_info
if "%3"=="" ( echo [error] svc info requires a name & goto :eof )
%PYTHON% "%CLI%" service info --name %3
goto :eof

:svc_create
if "%3"=="" ( echo [error] svc create requires name and owner & goto :eof )
if "%4"=="" ( echo [error] svc create requires name and owner & goto :eof )
:: %3=name  %4=owner  %5=flag(--template/--fork-from/--external-repo)  %6=value
set SVC_NAME=%3
set SVC_OWNER=%4
set SVC_EXTRA=
if not "%5"=="" set SVC_EXTRA=%5 %6
if "%5"=="" set SVC_EXTRA=--template springboot
%PYTHON% "%CLI%" service create --name !SVC_NAME! --owner !SVC_OWNER! !SVC_EXTRA!
goto :eof

:: ── Deploy ────────────────────────────────────────────────────────────────────
:deploy
if "%2"=="" ( echo [error] Usage: platform.bat deploy ^<service^> ^<version^> ^<env^> [--force] & goto :eof )
if "%3"=="" ( echo [error] Usage: platform.bat deploy ^<service^> ^<version^> ^<env^> [--force] & goto :eof )
if "%4"=="" ( echo [error] Usage: platform.bat deploy ^<service^> ^<version^> ^<env^> [--force] & goto :eof )
set FORCE_FLAG=
if "%5"=="--force" set FORCE_FLAG=--force
%PYTHON% "%CLI%" deploy --service %2 --version %3 --env %4 %FORCE_FLAG%
goto :eof

:: ── POC shortcuts ─────────────────────────────────────────────────────────────
:poc
if "%2"=="create"  goto :env_create
if "%2"=="destroy" goto :env_destroy
echo [error] Unknown poc subcommand: %2
echo   Usage: platform.bat poc create ^<name^> [base]
echo          platform.bat poc destroy ^<name^>
goto :eof
