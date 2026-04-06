#!/usr/bin/env bash
# delete.sh — Completely remove the AP3 test environment.
#
# ⚠️  DESTRUCTIVE: removes all containers, volumes, the k3d cluster,
# the Docker network, and testenv/.env. This cannot be undone.
#
# Usage:
#   bash testenv/delete.sh [--force]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
CLUSTER_NAME="ap3"

info()  { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
ok()    { echo -e "\033[1;32m[ OK ]\033[0m  $*"; }
warn()  { echo -e "\033[1;33m[WARN]\033[0m  $*"; }

# ── Podman: point to rootful socket (same as create.sh) ──────────────────────
if docker --version 2>/dev/null | grep -qi podman; then
    ROOTFUL_SOCK="/run/podman/podman.sock"
    if sudo test -S "$ROOTFUL_SOCK" 2>/dev/null; then
        sudo chmod 755 "$(dirname "$ROOTFUL_SOCK")" 2>/dev/null || true
        sudo chmod 666 "$ROOTFUL_SOCK" 2>/dev/null || true
        export DOCKER_HOST="unix://${ROOTFUL_SOCK}"
    fi
fi

dc() {
    local args=("-f" "$SCRIPT_DIR/docker-compose.yml")
    # env-file is optional for delete (services are going away anyway)
    [[ -f "$ENV_FILE" ]] && args=("--env-file" "$ENV_FILE" "${args[@]}")
    if docker compose version &>/dev/null 2>&1; then
        docker compose "${args[@]}" "$@"
    else
        docker-compose "${args[@]}" "$@"
    fi
}

# ── Confirm unless --force ────────────────────────────────────────────────────
if [[ "${1:-}" != "--force" ]]; then
    echo
    warn "This will PERMANENTLY DELETE:"
    echo "   • All containers  (Jenkins, SonarQube, PostgreSQL, Registry, Gitea)"
    echo "   • All volumes     (Jenkins config, SonarQube data, DB data, Gitea data)"
    echo "   • k3d cluster '$CLUSTER_NAME' and all workloads"
    echo "   • $ENV_FILE"
    echo
    read -r -p "Type 'yes' to confirm: " CONFIRM
    [[ "$CONFIRM" == "yes" ]] || { echo "Aborted."; exit 0; }
fi

# ── Compose down (remove containers + volumes) ────────────────────────────────
info "Removing compose services and volumes …"
dc down -v --remove-orphans 2>/dev/null || warn "docker compose down had errors (continuing)"

# Explicitly remove named volumes in case compose missed them
for vol in testenv_postgres_data testenv_sonarqube_data testenv_sonarqube_extensions \
           testenv_sonarqube_logs testenv_registry_data testenv_gitea_data \
           testenv_jenkins_data; do
    docker volume rm "$vol" 2>/dev/null && info "  removed volume $vol" || true
done
ok "Compose cleaned up"

# ── k3d cluster ───────────────────────────────────────────────────────────────
info "Deleting k3d cluster '$CLUSTER_NAME' …"
k3d cluster delete "$CLUSTER_NAME" 2>/dev/null && ok "k3d cluster deleted" \
    || warn "k3d cluster not found or already deleted"

# ── Jenkins image ─────────────────────────────────────────────────────────────
info "Removing Jenkins image ap3-jenkins:local …"
docker rmi ap3-jenkins:local 2>/dev/null && ok "Image removed" \
    || warn "Image not found or already removed"

# ── .env file ─────────────────────────────────────────────────────────────────
if [[ -f "$ENV_FILE" ]]; then
    cp "$ENV_FILE" "${ENV_FILE}.deleted.$(date +%s)" 2>/dev/null || true
    rm -f "$ENV_FILE"
    ok ".env removed (backup kept as .env.deleted.*)"
fi

# ── Restore platform.yaml backup if present ───────────────────────────────────
PLATFORM_BAK="$SCRIPT_DIR/../platform.yaml.bak"
if [[ -f "$PLATFORM_BAK" ]]; then
    info "Restoring platform.yaml from backup …"
    cp "$PLATFORM_BAK" "$SCRIPT_DIR/../platform.yaml"
    ok "platform.yaml restored"
fi

echo
ok "Test environment fully removed."
echo "Re-create with:  bash testenv/create.sh"
