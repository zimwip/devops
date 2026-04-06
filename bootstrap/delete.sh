#!/usr/bin/env bash
# bootstrap/delete.sh — Remove an AP3 platform instance from all tooling
#
# Reverses what bootstrap.sh did:
#   - Deletes GitHub/Gitea repos (platform, jenkins-shared-lib, extra libraries)
#   - Removes Jenkins pipeline jobs for all registered services
#   - Removes Jenkins global shared library configuration
#   - Deletes SonarQube projects for all registered services
#   - Removes the local platform instance directory
#
# Usage:
#   ./bootstrap/delete.sh                          # uses .bootstrap-state.yaml
#   ./bootstrap/delete.sh --config <yaml>          # uses config file
#   ./bootstrap/delete.sh --keep-repos             # skip GitHub/Gitea deletion
#   ./bootstrap/delete.sh --keep-jenkins           # skip Jenkins job deletion
#   ./bootstrap/delete.sh --keep-jenkins-lib       # skip Jenkins lib config removal
#   ./bootstrap/delete.sh --keep-sonar             # skip SonarQube deletion
#   ./bootstrap/delete.sh --keep-local             # skip local directory removal
#   ./bootstrap/delete.sh --yes                    # skip confirmation prompts
#
# Required environment variables (for the operations you don't skip):
#   GITHUB_TOKEN       GitHub/Gitea API token
#   JENKINS_USER       Jenkins username
#   JENKINS_TOKEN      Jenkins API token
#   SONARQUBE_TOKEN    SonarQube user token (for --keep-sonar if not skipped)

set -euo pipefail

BOOTSTRAP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLKIT_ROOT="$(dirname "${BOOTSTRAP_DIR}")"

RED="\033[31m"
BOLD="\033[1m"
RESET="\033[0m"

die() { echo -e "\n${RED}[error]${RESET} $*\n" >&2; exit 1; }

PYTHON=$(command -v python3 2>/dev/null || command -v python 2>/dev/null || "")
[[ -z "$PYTHON" ]] && die "Python not found."

echo ""
echo -e "  ${BOLD}AP3 Platform Delete${RESET}"
echo "  ──────────────────────────────────────────"
echo ""

# Pass all flags directly to delete.py
$PYTHON "${BOOTSTRAP_DIR}/scripts/delete.py" "$@"
