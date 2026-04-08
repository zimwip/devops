#!/usr/bin/env bash
# bootstrap/bootstrap.sh — Create a new AP3 platform instance
#
# This script is the entry point for setting up a new platform. It:
#   1. Copies the platform/ template to a new directory (default: ../platform)
#   2. Installs Python dependencies
#   3. Runs the interactive wizard to configure the platform instance
#   4. Creates GitHub repos, pushes jenkins-shared-lib and extra libraries
#   5. Configures Jenkins and creates the initial bootstrap commit
#
# Usage:
#   ./bootstrap/bootstrap.sh                                  # interactive
#   ./bootstrap/bootstrap.sh --config testenv/bootstrap-config.yaml  # non-interactive
#   ./bootstrap/bootstrap.sh --target /path/to/my-platform    # explicit target dir
#
# Bootstrap is idempotent: existing remote repos are reused, existing local
# platform-instance is reused (git not re-initialised). Run freely.
#
# To remove a previously bootstrapped platform:
#   ./bootstrap/delete.sh [--config ...]

set -euo pipefail

GREEN="\033[0;32m"
AMBER="\033[0;33m"
BLUE="\033[34m"
RED="\033[31m"
BOLD="\033[1m"
RESET="\033[0m"

step()    { echo -e "\n${BLUE}→ $*${RESET}"; }
success() { echo -e "${GREEN}✓ $*${RESET}"; }
warn()    { echo -e "${AMBER}! $*${RESET}"; }
die()     { echo -e "\n${RED}[error]${RESET} $*\n" >&2; exit 1; }

BOOTSTRAP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLKIT_ROOT="$(dirname "${BOOTSTRAP_DIR}")"
PLATFORM_SRC="${TOOLKIT_ROOT}/platform"

CONFIG_FILE=""
TARGET_DIR=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config|-c)
            [[ -z "${2:-}" ]] && die "--config requires a FILE argument"
            CONFIG_FILE="$2"; shift 2 ;;
        --target|-t)
            [[ -z "${2:-}" ]] && die "--target requires a DIR argument"
            TARGET_DIR="$2"; shift 2 ;;
        --help|-h)
            grep "^#" "$0" | head -20 | sed 's/^# \?//'
            exit 0 ;;
        *)  shift ;;
    esac
done

# ── Resolve target directory ──────────────────────────────────────────────────
# Priority: --target flag > bootstrap-config.yaml platform_target_dir > ../platform
if [[ -z "$TARGET_DIR" && -n "$CONFIG_FILE" ]]; then
    # Try to read platform_target_dir from config YAML (requires Python or grep)
    TARGET_DIR=$(python3 -c "
import yaml, sys
try:
    d = yaml.safe_load(open('${CONFIG_FILE}'))
    print(d.get('platform_target_dir', ''))
except Exception:
    pass
" 2>/dev/null || true)
fi

if [[ -z "$TARGET_DIR" ]]; then
    TARGET_DIR="${TOOLKIT_ROOT}/../platform"
fi

# Make absolute
TARGET_DIR="$(python3 -c "import os; print(os.path.abspath('${TARGET_DIR}'))")"

echo ""
echo -e "  ${BOLD}AP3 Platform Bootstrap${RESET}"
echo "  ──────────────────────────────────────────"
echo "  Toolkit:  ${TOOLKIT_ROOT}"
echo "  Template: ${PLATFORM_SRC}"
echo "  Target:   ${TARGET_DIR}"
echo ""

STATE_FILE="${BOOTSTRAP_DIR}/.bootstrap-state.yaml"

[[ ! -d "$PLATFORM_SRC" ]] && die "Platform template not found at ${PLATFORM_SRC}"

# ── Python ────────────────────────────────────────────────────────────────────
PYTHON=$(command -v python3 2>/dev/null || command -v python 2>/dev/null || "")
[[ -z "$PYTHON" ]] && die "Python not found. Install from https://python.org"

step "Installing bootstrap Python dependencies"
$PYTHON -m pip install -r "${PLATFORM_SRC}/scripts/requirements.txt" \
    --quiet --break-system-packages 2>/dev/null || \
$PYTHON -m pip install -r "${PLATFORM_SRC}/scripts/requirements.txt" --quiet
success "Python dependencies installed"

# ── Run wizard ────────────────────────────────────────────────────────────────
# The wizard handles: platform copy, env setup, repo creation, Jenkins config
WIZARD_ARGS=(
    "--platform-src"    "${PLATFORM_SRC}"
    "--platform-target" "${TARGET_DIR}"
)
[[ -n "$CONFIG_FILE" ]] && WIZARD_ARGS+=("--config" "$(realpath "${CONFIG_FILE}")")

step "Running bootstrap wizard"
$PYTHON "${BOOTSTRAP_DIR}/scripts/wizard.py" "${WIZARD_ARGS[@]}"

# ── Node dependencies in the platform instance ────────────────────────────────
step "Checking Node.js"
if command -v node &>/dev/null; then
    echo "  Found: $(node --version)"
    if [[ -d "${TARGET_DIR}/dashboard/frontend" ]]; then
        cd "${TARGET_DIR}/dashboard/frontend" && npm install --silent && cd -
        success "Node dependencies installed in platform instance"
    fi
else
    warn "Node.js not found — skipping frontend setup. Install from https://nodejs.org"
fi

# ── Initial platform commit ───────────────────────────────────────────────────
BOOTSTRAP_MARKER="chore: initial AP3 platform bootstrap"

step "Creating initial platform commit"
cd "${TARGET_DIR}"
git config user.email "platform-bootstrap@ap3.local" 2>/dev/null || true
git config user.name  "AP3 Bootstrap" 2>/dev/null || true
git add --all
if git diff --cached --quiet; then
    warn "Nothing to commit — platform may already have a commit."
else
    git commit -m "${BOOTSTRAP_MARKER}

Platform: AP3
Bootstrapped: $(date -u '+%Y-%m-%dT%H:%M:%SZ')

This is the initial bootstrap commit. Every subsequent change to envs/
is a separate commit forming the deployment audit log."
    success "Initial commit created"
fi

# ── Push to origin ─────────────────────────────────────────────────────────────
if git remote get-url origin &>/dev/null 2>&1; then
    step "Pushing to origin"
    PUSH_URL="$(git remote get-url origin)"
    PUSH_OK=false
    if git push -u origin main 2>/dev/null; then
        PUSH_OK=true
    else
        # Remote may have a stale commit (previous bootstrap run, or Gitea default
        # branch init).  Force-push is safe here: this repo belongs to this
        # bootstrap instance and the local content is authoritative.
        warn "Normal push rejected — remote has diverged history (stale bootstrap?)."
        warn "Force-pushing local content to origin..."
        if git push --force -u origin main; then
            PUSH_OK=true
        fi
    fi

    if $PUSH_OK; then
        success "Pushed to ${PUSH_URL}"

        # ── Re-clone for a clean working copy ─────────────────────────────────
        # Strip embedded credentials from the push URL to get a clean clone URL.
        CLONE_URL=$(python3 -c "
from urllib.parse import urlparse, urlunparse
u = urlparse('${PUSH_URL}')
netloc = u.hostname + (':' + str(u.port) if u.port else '')
print(urlunparse(u._replace(netloc=netloc)))
")
        step "Replacing initialised repo with a clean clone"
        PARENT_DIR="$(dirname "${TARGET_DIR}")"
        CLONE_NAME="$(basename "${TARGET_DIR}")"
        cd "${PARENT_DIR}"
        rm -rf "${TARGET_DIR}"
        if git clone "${CLONE_URL}" "${CLONE_NAME}"; then
            success "Platform instance cloned at ${TARGET_DIR}"
            TARGET_DIR="${PARENT_DIR}/${CLONE_NAME}"
            # Re-install Node deps (node_modules not committed)
            if command -v node &>/dev/null && [[ -d "${TARGET_DIR}/dashboard/frontend" ]]; then
                step "Installing Node dependencies in cloned instance"
                cd "${TARGET_DIR}/dashboard/frontend" && npm install --silent && cd -
                success "Node dependencies installed"
            fi
        else
            warn "Clone failed — the push succeeded but you will need to clone manually:"
            warn "  git clone ${CLONE_URL} ${TARGET_DIR}"
        fi
    else
        warn "git push failed — local commit created successfully."
        warn "Push manually: cd ${TARGET_DIR} && git push --force -u origin main"
    fi
fi

# ── Node deps (no-remote path) ────────────────────────────────────────────────
# If there was no origin to push/clone, node_modules was already installed above.
# Nothing extra needed here.

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "  ──────────────────────────────────────────"
echo -e "  ${GREEN}Platform bootstrapped successfully!${RESET}"
echo ""
echo "  Platform instance:  ${TARGET_DIR}"
echo "  Bootstrap state:    ${STATE_FILE}"
echo ""
echo "  Next steps:"
echo "    cd ${TARGET_DIR}"
echo "    cp env.example .env  &&  edit .env"
echo "    ./platform.sh env list"
echo "    make dev                     # Start API + dashboard"
echo ""
echo "  To remove this platform:   cd ${TOOLKIT_ROOT} && ./bootstrap/delete.sh"
echo "  ──────────────────────────────────────────"
echo ""
