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
warn()  { echo -e "\033[1;33m[WARN]\033[0m  $*"; }
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

# ── Re-register Gitea DNS in k3d ─────────────────────────────────────────────
# Gitea joins k3d-ap3 via docker-compose.yml; its IP changes on every restart.
# Update the headless Service+Endpoints in jenkins-builds so agent pods
# resolve http://gitea:3000 immediately via kube-dns.
info "Updating Gitea DNS (Service+Endpoints in ${BUILDS_NS:-jenkins-builds}) …"

# Gitea joins k3d-ap3 via docker-compose.yml networks — no manual connect needed.
# Use container inspect (not network inspect — Podman's network inspect omits
# the Containers section).  Try user docker first, then sudo (system Podman).
_gitea_ip() {
    local json
    json=$(docker inspect gitea 2>/dev/null)
    [[ -z "$json" || "$json" == "[]" ]] && json=$(sudo docker inspect gitea 2>/dev/null)
    echo "$json" | python3 -c "
import sys, json
data = json.load(sys.stdin)
c = data[0] if data else {}
nets = c.get('NetworkSettings', {}).get('Networks', {})
for name, info in nets.items():
    if 'k3d' in name:
        ip = info.get('IPAddress', '')
        if ip: print(ip); break
" 2>/dev/null
}
GITEA_K3D_IP=$(_gitea_ip || true)

BUILDS_NS="jenkins-builds"

if [[ -z "$GITEA_K3D_IP" ]]; then
    warn "GITEA DNS NOT UPDATED — builds will fail to clone from gitea."
    warn "Fix: sudo docker inspect gitea | grep -A5 k3d — check gitea is on k3d-ap3 network"
    exit 1
fi

# Update the headless Service+Endpoints so agent pods immediately resolve
# the fresh container IP via kube-dns (no ConfigMap file-sync latency).
kubectl apply -n "${BUILDS_NS}" -f - <<EOF
apiVersion: v1
kind: Service
metadata:
  name: gitea
spec:
  clusterIP: None
  ports:
  - name: http
    port: 3000
    protocol: TCP
    targetPort: 3000
---
apiVersion: v1
kind: Endpoints
metadata:
  name: gitea
subsets:
- addresses:
  - ip: ${GITEA_K3D_IP}
  ports:
  - name: http
    port: 3000
    protocol: TCP
EOF
ok "gitea Service+Endpoints updated in ${BUILDS_NS}: gitea → ${GITEA_K3D_IP}"

# ── Re-apply k8s resources that may be lost after cluster recreate ─────────────
info "Refreshing k8s ConfigMaps / Secrets …"
kubectl apply -f "$SCRIPT_DIR/k8s/maven-settings-configmap.yaml" 2>/dev/null || true
if [[ -n "${NEXUS_PASSWORD:-}" ]]; then
    kubectl create secret generic nexus-credentials \
        --namespace jenkins-builds \
        --from-literal=password="${NEXUS_PASSWORD}" \
        --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null || true
fi

ok "All services starting. Check status with:"
echo "   docker compose -f testenv/docker-compose.yml ps"
echo "   kubectl cluster-info"
echo
echo "   Jenkins:   http://localhost:8080  (${JENKINS_ADMIN_USER:-admin} / ${JENKINS_ADMIN_PASSWORD:-see .env})"
echo "   SonarQube: http://localhost:9000  (admin / ${SONAR_ADMIN_PASSWORD:-see .env})"
echo "   Nexus:     http://localhost:8081  (admin / ${NEXUS_PASSWORD:-see .env})"
