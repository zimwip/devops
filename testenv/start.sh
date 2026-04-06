#!/usr/bin/env bash
# start.sh — Start a previously created AP3 test environment.
#
# Use this after a reboot or after running stop.sh.
# Does NOT re-run setup — all credentials remain as they were.
#
# Usage:
#   bash testenv/start.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
CLUSTER_NAME="ap3"

info()  { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
ok()    { echo -e "\033[1;32m[ OK ]\033[0m  $*"; }
die()   { echo -e "\033[1;31m[ERR ]\033[0m  $*" >&2; exit 1; }

dc() {
    local args=("--env-file" "$ENV_FILE" "-f" "$SCRIPT_DIR/docker-compose.yml")
    if docker compose version &>/dev/null 2>&1; then
        docker compose "${args[@]}" "$@"
    else
        docker-compose "${args[@]}" "$@"
    fi
}

[[ -f "$ENV_FILE" ]] || die ".env not found — run create.sh first"
source "$ENV_FILE"

# Rootful Podman socket — required for k3d and compose services
if docker --version 2>/dev/null | grep -qi podman; then
    ROOTFUL_SOCK="/run/podman/podman.sock"
    sudo systemctl enable --now podman.socket 2>/dev/null || true
    for _i in $(seq 1 10); do sudo test -S "$ROOTFUL_SOCK" && break; sleep 1; done
    if sudo test -S "$ROOTFUL_SOCK"; then
        sudo chmod 755 "$(dirname "$ROOTFUL_SOCK")"
        sudo chmod 666 "$ROOTFUL_SOCK"
        export DOCKER_HOST="unix://${ROOTFUL_SOCK}"
        ok "Podman socket ready"
    else
        warn "Rootful Podman socket not found — things may fail"
    fi
fi

# SonarQube Elasticsearch kernel requirement
CURRENT_MAP=$(sysctl -n vm.max_map_count 2>/dev/null || echo 0)
if [[ "$CURRENT_MAP" -lt 524288 ]]; then
    info "Applying vm.max_map_count=524288 for SonarQube …"
    sudo sysctl -w vm.max_map_count=524288
fi

# ── k3d ──────────────────────────────────────────────────────────────────────
info "Starting k3d cluster '$CLUSTER_NAME' …"
if k3d cluster list 2>/dev/null | grep -q "^${CLUSTER_NAME}[[:space:]]"; then
    k3d cluster start "$CLUSTER_NAME"
    k3d kubeconfig merge "$CLUSTER_NAME" --kubeconfig-switch-context
    ok "k3d cluster started"
else
    die "k3d cluster '$CLUSTER_NAME' not found — run create.sh first"
fi

# ── Compose services ─────────────────────────────────────────────────────────
info "Starting compose services …"
dc up -d

ok "All services starting. Check status with:"
echo "   docker compose -f testenv/docker-compose.yml ps"
echo "   kubectl cluster-info"
echo
echo "   Jenkins:   http://localhost:8080  (${JENKINS_ADMIN_USER:-admin} / ${JENKINS_ADMIN_PASSWORD:-see .env})"
echo "   SonarQube: http://localhost:9000  (admin / ${SONAR_ADMIN_PASSWORD:-see .env})"
