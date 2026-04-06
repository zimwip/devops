#!/usr/bin/env bash
# platform.sh — AP3 Platform CLI launcher (Linux / macOS)
# Equivalent of platform.bat for Unix systems.
#
# Usage:
#   ./platform.sh help
#   ./platform.sh install
#   ./platform.sh dev
#   ./platform.sh dev-api
#   ./platform.sh dev-ui
#   ./platform.sh test
#   ./platform.sh build
#
#   ./platform.sh env list
#   ./platform.sh env info <name>
#   ./platform.sh env create <name> [base] [namespace] [cluster] [platform]
#   ./platform.sh env destroy <name>
#   ./platform.sh env diff <from> <to>
#   ./platform.sh env extend <name> [days]
#
#   ./platform.sh svc list
#   ./platform.sh svc info <name>
#   ./platform.sh svc create <name> <owner>                         (template: springboot)
#   ./platform.sh svc create <name> <owner> --template <tpl>        (springboot|react|python-api)
#   ./platform.sh svc create <name> <owner> --fork-from <service>   (fork AP3 service)
#   ./platform.sh svc create <name> <owner> --external-repo <url>   (register existing repo)
#
#   ./platform.sh deploy <service> <version> <env> [--force]
#
#   ./platform.sh cluster list
#   ./platform.sh cluster info <name>
#   ./platform.sh cluster add <name> --platform openshift --api-url <url> --context <ctx>
#   ./platform.sh cluster add <name> --platform aws --region <r> --cluster-name <n>
#   ./platform.sh cluster remove <name> [--force]
#
#   ./platform.sh config show
#   ./platform.sh config set [--github-org <org>] [--github-url <url>] [--jenkins-url <url>]
#
#   ./platform.sh history [--env <e>] [--service <s>] [--type <t>] [--limit <n>]
#
#   ./platform.sh poc create <name> [base] [namespace]
#   ./platform.sh poc destroy <name>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-$(command -v python3 2>/dev/null || command -v python)}"
CLI="${SCRIPT_DIR}/scripts/platform_cli.py"
API_PORT="${PORT:-5173}"
UI_PORT="${UI_PORT:-5174}"

# ── Colour helpers ─────────────────────────────────────────────────────────────
GREEN="\033[0;32m"
AMBER="\033[0;33m"
BLUE="\033[0;34m"
RED="\033[0;31m"
RESET="\033[0m"
ok()   { echo -e "  ${GREEN}✓${RESET}  $*"; }
info() { echo -e "  ${BLUE}→${RESET}  $*"; }
warn() { echo -e "  ${AMBER}!${RESET}  $*"; }
die()  { echo -e "  ${RED}[error]${RESET} $*" >&2; exit 1; }

run_cli() { "$PYTHON" "$CLI" "$@"; }

CMD="${1:-help}"; shift || true

case "$CMD" in

# ── Help ───────────────────────────────────────────────────────────────────────
help|--help|-h)
  echo ""
  echo "  AP3 Platform — CLI launcher"
  echo "  ────────────────────────────────────────────────────────────────"
  echo "  ./platform.sh install             Install Python + Node dependencies"
  echo "  ./platform.sh dev                 Start API :${API_PORT} + UI :${UI_PORT} (background)"
  echo "  ./platform.sh dev-api             FastAPI only (foreground)"
  echo "  ./platform.sh dev-ui              React dev server only (foreground)"
  echo "  ./platform.sh test                Run pytest suite"
  echo "  ./platform.sh build               Build React frontend"
  echo ""
  echo "  Environment management:"
  echo "  ./platform.sh env list"
  echo "  ./platform.sh env info <name>"
  echo "  ./platform.sh env create <name> [base] [ns] [cluster] [platform]"
  echo "  ./platform.sh env destroy <name>"
  echo "  ./platform.sh env diff <from> <to>"
  echo "  ./platform.sh env extend <name> [days]"
  echo ""
  echo "  Service management:"
  echo "  ./platform.sh svc list"
  echo "  ./platform.sh svc info <name>"
  echo "  ./platform.sh svc jenkins-register <name>              # create/recreate Jenkins pipeline"
  echo "  ./platform.sh svc create <name> <owner>                        # template (springboot)"
  echo "  ./platform.sh svc create <name> <owner> --template react       # choose template"
  echo "  ./platform.sh svc create <name> <owner> --fork-from <src>      # fork AP3 service"
  echo "  ./platform.sh svc create <name> <owner> --external-repo <url>  # register external"
  echo ""
  echo "  Deployments:"
  echo "  ./platform.sh deploy <service> <version> <env> [--force]"
  echo ""
  echo "  Cluster management:"
  echo "  ./platform.sh cluster list"
  echo "  ./platform.sh cluster info <name>"
  echo "  ./platform.sh cluster add <name> --platform openshift --api-url <url> --context <ctx>"
  echo "  ./platform.sh cluster add <name> --platform aws --region <r> --cluster-name <n>"
  echo "  ./platform.sh cluster remove <name> [--force]"
  echo ""
  echo "  Configuration:"
  echo "  ./platform.sh config show"
  echo "  ./platform.sh config set [--github-org <org>] [--jenkins-url <url>] ..."
  echo ""
  echo "  Audit log:"
  echo "  ./platform.sh history [--env <e>] [--service <s>] [--type <t>] [--limit <n>]"
  echo ""
  echo "  POC shortcuts (alias for env create/destroy):"
  echo "  ./platform.sh poc create <name> [base] [namespace]"
  echo "  ./platform.sh poc destroy <name>"
  echo ""
  ;;

# ── Install ────────────────────────────────────────────────────────────────────
install)
  info "Installing Python dependencies"
  "$PYTHON" -m pip install -r "${SCRIPT_DIR}/scripts/requirements.txt" \
      --quiet --break-system-packages 2>/dev/null || \
  "$PYTHON" -m pip install -r "${SCRIPT_DIR}/scripts/requirements.txt" --quiet
  ok "Python dependencies installed"

  info "Installing Node dependencies"
  if command -v node &>/dev/null; then
    cd "${SCRIPT_DIR}/dashboard/frontend" && npm install --silent
    ok "Node dependencies installed"
    cd "${SCRIPT_DIR}"
  else
    warn "Node.js not found — skipping (install from https://nodejs.org)"
  fi
  echo ""
  ok "Ready. Run: ./platform.sh dev"
  ;;

# ── Dev servers ────────────────────────────────────────────────────────────────
dev)
  info "Starting API (:${API_PORT}) and UI (:${UI_PORT}) in background"
  echo "  API → http://localhost:${API_PORT}  (Swagger: /docs)"
  echo "  UI  → http://localhost:${UI_PORT}"
  echo ""

  PYTHONPATH="${SCRIPT_DIR}/scripts:${SCRIPT_DIR}/dashboard/backend" \
    uvicorn app:app --reload --port "${API_PORT}" \
    --app-dir "${SCRIPT_DIR}/dashboard/backend" &
  API_PID=$!

  sleep 1

  cd "${SCRIPT_DIR}/dashboard/frontend"
  npm run dev -- --port "${UI_PORT}" &
  UI_PID=$!
  cd "${SCRIPT_DIR}"

  echo "  PIDs: API=${API_PID}  UI=${UI_PID}"
  echo "  Stop: kill ${API_PID} ${UI_PID}  (or Ctrl-C)"
  wait
  ;;

dev-api)
  info "Starting FastAPI on :${API_PORT}"
  echo "  Swagger UI: http://localhost:${API_PORT}/docs"
  PYTHONPATH="${SCRIPT_DIR}/scripts:${SCRIPT_DIR}/dashboard/backend" \
    uvicorn app:app --reload --port "${API_PORT}" \
    --app-dir "${SCRIPT_DIR}/dashboard/backend"
  ;;

dev-ui)
  info "Starting React dev server on :${UI_PORT}"
  cd "${SCRIPT_DIR}/dashboard/frontend"
  npm run dev -- --port "${UI_PORT}"
  ;;

# ── Test ───────────────────────────────────────────────────────────────────────
test)
  info "Running backend tests"
  PYTHONPATH="${SCRIPT_DIR}/scripts:${SCRIPT_DIR}/dashboard/backend" \
    "$PYTHON" -m pytest "${SCRIPT_DIR}/dashboard/backend/tests/" -v --tb=short
  ;;

# ── Build ──────────────────────────────────────────────────────────────────────
build)
  info "Building React frontend"
  cd "${SCRIPT_DIR}/dashboard/frontend"
  npm run build
  cd "${SCRIPT_DIR}"
  ok "Frontend built → dashboard/frontend/dist/"
  ;;

# ── Env commands ───────────────────────────────────────────────────────────────
env)
  SUB="${1:-}"; shift || true
  case "$SUB" in
    list)    run_cli env list ;;
    info)    [[ -z "${1:-}" ]] && die "env info requires a name"
             run_cli env info --name "$1" ;;
    create)
      [[ -z "${1:-}" ]] && die "env create requires a name"
      NAME="$1"; BASE="${2:-dev}"; NS="${3:-}"; CLUSTER="${4:-}"; PLATFORM="${5:-}"
      ARGS=(env create --name "$NAME" --base "$BASE")
      [[ -n "$NS"       ]] && ARGS+=(--namespace "$NS")
      [[ -n "$CLUSTER"  ]] && ARGS+=(--cluster "$CLUSTER")
      [[ -n "$PLATFORM" ]] && ARGS+=(--platform "$PLATFORM")
      run_cli "${ARGS[@]}"
      ;;
    destroy) [[ -z "${1:-}" ]] && die "env destroy requires a name"
             run_cli env destroy --name "$1" ;;
    diff)    [[ -z "${1:-}" || -z "${2:-}" ]] && die "env diff requires two env names"
             run_cli env diff --from "$1" --to "$2" ;;
    extend)  [[ -z "${1:-}" ]] && die "env extend requires a name"
             DAYS="${2:-14}"
             run_cli env extend --name "$1" --ttl-days "$DAYS" ;;
    *)       die "Unknown env subcommand '${SUB}'. Use: list|info|create|destroy|diff|extend" ;;
  esac
  ;;

# ── Svc commands ───────────────────────────────────────────────────────────────
svc)
  SUB="${1:-}"; shift || true
  case "$SUB" in
    list)  run_cli service list ;;
    info)  [[ -z "${1:-}" ]] && die "svc info requires a name"
           run_cli service info --name "$1" ;;
    create)
      # svc create <n> <owner> [--template|--fork-from|--external-repo] [--dry-run]
      [[ -z "${1:-}" ]] && die "svc create requires a name"
      [[ -z "${2:-}" ]] && die "svc create requires an owner"
      NAME="$1"; OWNER="$2"; shift 2
      # --dry-run is a global flag: must sit between "python" and the subcommand name.
      GLOBAL_FLAGS=(); EXTRA=()
      for arg in "$@"; do
        [[ "$arg" == "--dry-run" ]] && GLOBAL_FLAGS+=("--dry-run") || EXTRA+=("$arg")
      done
      if [[ ${#EXTRA[@]} -eq 0 ]]; then
        "$PYTHON" "$CLI" "${GLOBAL_FLAGS[@]}" service create \
            --name "$NAME" --owner "$OWNER" --template springboot
      else
        "$PYTHON" "$CLI" "${GLOBAL_FLAGS[@]}" service create \
            --name "$NAME" --owner "$OWNER" "${EXTRA[@]}"
      fi
      ;;
    jenkins-register)
      [[ -z "${1:-}" ]] && die "svc jenkins-register requires a service name"
      run_cli service jenkins-register "$1" ;;
    *)  die "Unknown svc subcommand '${SUB}'. Use: list|info|create|jenkins-register" ;;
  esac
  ;;

# ── Deploy ─────────────────────────────────────────────────────────────────────
deploy)
  [[ -z "${1:-}" || -z "${2:-}" || -z "${3:-}" ]] && \
    die "Usage: ./platform.sh deploy <service> <version> <env> [--force]"
  run_cli deploy --service "$1" --version "$2" --env "$3" "${4:+$4}"
  ;;

# ── Cluster commands ───────────────────────────────────────────────────────────
cluster)
  SUB="${1:-}"; shift || true
  case "$SUB" in
    list)   run_cli cluster list ;;
    info)   [[ -z "${1:-}" ]] && die "cluster info requires a name"
            run_cli cluster info --name "$1" ;;
    add)    [[ -z "${1:-}" ]] && die "cluster add requires a name"
            # Pass all remaining args directly — they contain --platform, --api-url, etc.
            run_cli cluster add --name "$1" "${@:2}" ;;
    remove) [[ -z "${1:-}" ]] && die "cluster remove requires a name"
            run_cli cluster remove --name "$1" "${2:+$2}" ;;
    *)      die "Unknown cluster subcommand '${SUB}'. Use: list|info|add|remove" ;;
  esac
  ;;

# ── Config commands ────────────────────────────────────────────────────────────
config)
  SUB="${1:-show}"; shift || true
  case "$SUB" in
    show) run_cli config show ;;
    set)  run_cli config set "$@" ;;
    *)    die "Unknown config subcommand '${SUB}'. Use: show|set" ;;
  esac
  ;;

# ── History ────────────────────────────────────────────────────────────────────
history)
  run_cli history "$@"
  ;;

# ── POC shortcuts ──────────────────────────────────────────────────────────────
poc)
  SUB="${1:-}"; shift || true
  case "$SUB" in
    create)
      [[ -z "${1:-}" ]] && die "poc create requires a name"
      NAME="$1"; BASE="${2:-dev}"; NS="${3:-}"
      ARGS=(env create --name "$NAME" --base "$BASE")
      [[ -n "$NS" ]] && ARGS+=(--namespace "$NS")
      run_cli "${ARGS[@]}"
      ;;
    destroy)
      [[ -z "${1:-}" ]] && die "poc destroy requires a name"
      run_cli env destroy --name "$1"
      ;;
    *)  die "Unknown poc subcommand '${SUB}'. Use: create|destroy" ;;
  esac
  ;;

# ── Unknown ────────────────────────────────────────────────────────────────────
*)
  die "Unknown command '${CMD}'. Run: ./platform.sh help"
  ;;

esac
