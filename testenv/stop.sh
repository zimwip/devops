#!/usr/bin/env bash
# stop.sh — Stop the AP3 test environment without deleting data.
#
# All data (Jenkins config, SonarQube projects, k8s resources) is preserved
# in Docker named volumes and the k3d cluster. Use start.sh to resume.
#
# Usage:
#   bash testenv/stop.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
CLUSTER_NAME="ap3"

info() { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
ok()   { echo -e "\033[1;32m[ OK ]\033[0m  $*"; }

# Rootful Podman socket — needed to reach compose containers
if docker --version 2>/dev/null | grep -qi podman; then
    ROOTFUL_SOCK="/run/podman/podman.sock"
    if sudo test -S "$ROOTFUL_SOCK" 2>/dev/null; then
        sudo chmod 755 "$(dirname "$ROOTFUL_SOCK")" 2>/dev/null || true
        sudo chmod 666 "$ROOTFUL_SOCK" 2>/dev/null || true
        export DOCKER_HOST="unix://${ROOTFUL_SOCK}"
    fi
fi

dc() {
    local args=("--env-file" "$ENV_FILE" "-f" "$SCRIPT_DIR/docker-compose.yml")
    if docker compose version &>/dev/null 2>&1; then
        docker compose "${args[@]}" "$@"
    else
        docker-compose "${args[@]}" "$@"
    fi
}

info "Stopping compose services …"
dc stop
ok "Compose services stopped"

info "Stopping k3d cluster '$CLUSTER_NAME' …"
k3d cluster stop "$CLUSTER_NAME" 2>/dev/null && ok "k3d stopped" || echo "  (cluster not found or already stopped)"

echo
echo "Environment stopped. Data preserved in Docker volumes."
echo "Resume with:  bash testenv/start.sh"
