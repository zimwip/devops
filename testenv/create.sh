#!/usr/bin/env bash
# create.sh — Build and start the AP3 local test environment.
#
# Services started:
#   Gitea      :3000  — local GitHub (Git hosting + compatible REST API)
#   SonarQube  :9000  — code quality gate
#   Registry   :5000  — local container registry (simulates Artifactory)
#   Jenkins    :8080  — CI/CD (Kubernetes plugin → k3d build pods)
#   k3d              — lightweight Kubernetes (replaces OpenShift)
#
# Idempotent: safe to re-run after a partial failure.
#
# Usage:
#   cd /path/to/platform
#   bash testenv/create.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
USERS_FILE="$SCRIPT_DIR/.users"

CLUSTER_NAME="ap3"
K3D_API_HOST_PORT="6550"
BUILDS_NS="jenkins-builds"

# ─── Helpers ──────────────────────────────────────────────────────────────────

info()  { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
ok()    { echo -e "\033[1;32m[ OK ]\033[0m  $*"; }
warn()  { echo -e "\033[1;33m[WARN]\033[0m  $*"; }
die()   { echo -e "\033[1;31m[ERR ]\033[0m  $*" >&2; exit 1; }
step()  { echo -e "\n\033[1;36m══ $* ══\033[0m"; }

update_env() {
    local key="$1" value="$2"
    if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
    else
        echo "${key}=${value}" >> "$ENV_FILE"
    fi
}

update_users() {
    local key="$1" value="$2"
    if grep -q "^${key}=" "$USERS_FILE" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${value}|" "$USERS_FILE"
    else
        echo "${key}=${value}" >> "$USERS_FILE"
    fi
}

wait_http() {
    local url="$1" label="$2" tries="${3:-60}" delay="${4:-5}"
    info "Waiting for $label …"
    for i in $(seq 1 "$tries"); do
        if curl -sf "$url" >/dev/null 2>&1; then ok "$label responded"; return 0; fi
        printf "."
        sleep "$delay"
    done
    echo
    die "$label did not become available after $((tries * delay))s"
}

# ─── Runtime detection ────────────────────────────────────────────────────────
# Must happen before pre-flight so DOCKER_HOST is set when we test docker info.

IS_PODMAN=false
if docker --version 2>/dev/null | grep -qi podman; then
    IS_PODMAN=true
fi

if $IS_PODMAN; then
    ROOTFUL_SOCK="/run/podman/podman.sock"

    # Start rootful Podman socket — k3d mounts this into its tools container,
    # so the rootless per-session socket (/run/user/<uid>/...) cannot be used.
    if ! sudo systemctl is-active podman.socket &>/dev/null; then
        info "Enabling + starting rootful Podman socket …"
        sudo systemctl enable --now podman.socket 2>/dev/null || \
            warn "podman.socket enable returned non-zero (continuing to check)"
    fi

    # Wait up to 15 s for the socket file to materialise.
    # Use `sudo test` because /run/podman/ is root-owned and non-root bash
    # [[ -S ... ]] cannot see inside it until chmod 666 is applied.
    for _i in $(seq 1 15); do
        sudo test -S "$ROOTFUL_SOCK" && break
        sleep 1
    done

    if ! sudo test -S "$ROOTFUL_SOCK"; then
        echo
        warn "Socket not found — systemd status:"
        sudo systemctl status podman.socket --no-pager -l 2>/dev/null || true
        echo
        warn "Contents of /run/podman/ (if it exists):"
        sudo ls -la /run/podman/ 2>/dev/null || echo "  (directory does not exist)"
        echo
        die "Rootful Podman socket not found at $ROOTFUL_SOCK.
  Run these commands and then re-run create.sh:
    sudo systemctl enable --now podman.socket
  If the service is failing, inspect it:
    sudo journalctl -u podman.socket -n 30
  Ensure podman is installed:
    sudo dnf install -y podman"
    fi

    # Make the socket world-accessible.
    # Two steps needed:
    #   1. The /run/podman/ directory is drwx------ (root only) — non-root
    #      processes can't traverse into it even if the socket file is 666.
    #   2. The socket file itself needs rw for all so k3d's Go client can connect.
    # Both reset automatically when the podman.socket service restarts — safe for dev.
    sudo chmod 755 "$(dirname "$ROOTFUL_SOCK")"
    sudo chmod 666 "$ROOTFUL_SOCK"

    export DOCKER_HOST="unix://${ROOTFUL_SOCK}"
    export K3D_FIX_DNS=1
    ok "Container runtime: rootful Podman  (DOCKER_HOST=$DOCKER_HOST)"
fi

# ─── Compose helper ───────────────────────────────────────────────────────────

dc() {
    local compose_file="$SCRIPT_DIR/docker-compose.yml"
    if docker compose version &>/dev/null 2>&1; then
        # Compose v2: supports multiple --env-file flags
        docker compose \
            --env-file "$ENV_FILE" --env-file "$USERS_FILE" \
            -f "$compose_file" "$@"
    elif command -v docker-compose &>/dev/null; then
        # Compose v1: only one --env-file; export both files instead
        set -a; source "$ENV_FILE"; [[ -f "$USERS_FILE" ]] && source "$USERS_FILE"; set +a
        docker-compose -f "$compose_file" "$@"
    else
        # podman-compose: export vars since it ignores --env-file
        set -a; source "$ENV_FILE"; [[ -f "$USERS_FILE" ]] && source "$USERS_FILE"; set +a
        podman-compose -f "$compose_file" "$@"
    fi
}

# ═══════════════════════════════════════════════════════════════════════════════
# PRE-FLIGHT CHECKS
# Run every check, collect all failures, then show the full fix list and stop.
# Only blocking issues prevent continuation; warnings are printed and skipped.
# ═══════════════════════════════════════════════════════════════════════════════

step "Pre-flight checks"

BLOCKING=()   # issues that require manual action before re-running
AUTO_FIXED=() # issues fixed automatically by this script

# ── Helper: check + record ────────────────────────────────────────────────────
require() {
    # require "label" "test command" "fix instructions"
    local label="$1" test_cmd="$2" fix="$3"
    if eval "$test_cmd" &>/dev/null 2>&1; then
        ok "$label"
    else
        warn "MISSING: $label"
        BLOCKING+=("$label|$fix")
    fi
}

autofix() {
    # autofix "label" "test command" "fix command" "fix description"
    local label="$1" test_cmd="$2" fix_cmd="$3" fix_desc="$4"
    if eval "$test_cmd" &>/dev/null 2>&1; then
        ok "$label"
    else
        warn "$label — auto-fixing …"
        if eval "$fix_cmd" &>/dev/null 2>&1; then
            AUTO_FIXED+=("$label: $fix_desc")
            ok "$label (fixed)"
        else
            BLOCKING+=("$label|$fix_desc")
        fi
    fi
}

port_free() {
    local port="$1" use="$2"
    if ss -tlnp 2>/dev/null | grep -q ":${port} "; then
        PROC=$(ss -tlnp 2>/dev/null | grep ":${port} " \
            | grep -oP 'users:\(\("\K[^"]+' | head -1 || echo "unknown")
        PID=$(ss -tlnp 2>/dev/null | grep ":${port} " \
            | grep -oP 'pid=\K[0-9]+' | head -1 || echo "")
        if [[ -n "$PID" ]]; then
            BLOCKING+=("Port ${port}/tcp already in use  (process: ${PROC}, pid: ${PID})  — needed for ${use}|sudo kill ${PID}
# or to see what is using it:
sudo ss -tlnp | grep :${port}")
        else
            BLOCKING+=("Port ${port}/tcp already in use  (process: ${PROC})  — needed for ${use}|sudo ss -tlnp | grep :${port}
# find the PID in the output above, then:
sudo kill <PID>")
        fi
        warn "Port $port in use (${PROC}) — needed for $use"
    else
        ok "Port $port free  ($use)"
    fi
}

# ── Tool availability ─────────────────────────────────────────────────────────
require "docker CLI" \
    "command -v docker" \
    "sudo dnf install podman-docker"

require "curl" \
    "command -v curl" \
    "sudo dnf install curl"

require "openssl" \
    "command -v openssl" \
    "sudo dnf install openssl"

require "python3" \
    "command -v python3" \
    "sudo dnf install python3"

require "ss (socket statistics)" \
    "command -v ss" \
    "sudo dnf install iproute"

# ── Container daemon ──────────────────────────────────────────────────────────
if $IS_PODMAN; then
    require "rootful Podman socket at /run/podman/podman.sock" \
        "sudo test -S /run/podman/podman.sock" \
        "sudo systemctl enable --now podman.socket
# If still missing after that:
sudo journalctl -u podman.socket -n 20"

    require "container daemon accessible via DOCKER_HOST" \
        "sudo chmod 666 /run/podman/podman.sock && DOCKER_HOST=unix:///run/podman/podman.sock docker info" \
        "sudo chmod 666 /run/podman/podman.sock
# or restart the socket:
sudo systemctl restart podman.socket && sudo chmod 666 /run/podman/podman.sock"
else
    require "Docker daemon accessible" \
        "docker info" \
        "sudo systemctl enable --now docker
sudo usermod -aG docker \$USER
newgrp docker"
fi

# ── Kernel settings (auto-fixed if possible) ──────────────────────────────────
autofix \
    "vm.max_map_count ≥ 524288 (required by SonarQube/Elasticsearch)" \
    "[[ \$(sysctl -n vm.max_map_count 2>/dev/null || echo 0) -ge 524288 ]]" \
    "sudo sysctl -w vm.max_map_count=524288" \
    "sudo sysctl -w vm.max_map_count=524288
echo 'vm.max_map_count=524288' | sudo tee /etc/sysctl.d/99-ap3.conf"

# ── Required ports ────────────────────────────────────────────────────────────
port_free 3000            "Gitea"
port_free 5000            "Registry"
port_free 8080            "Jenkins UI"
port_free 9000            "SonarQube"
port_free 50000           "Jenkins JNLP (agent connect-back)"
# Skip the API port check when the k3d cluster already exists — the port is
# legitimately held by that cluster (conmon on Podman, dockerd on Docker).
if ! k3d cluster list 2>/dev/null | grep -q "^${CLUSTER_NAME}[[:space:]]"; then
    port_free "$K3D_API_HOST_PORT"  "k3d Kubernetes API"
else
    ok "Port ${K3D_API_HOST_PORT} in use by existing k3d cluster '${CLUSTER_NAME}' (expected)"
fi

# ── k3d + kubectl (install automatically if absent) ──────────────────────────
if ! command -v k3d &>/dev/null; then
    info "k3d not found — installing v5.7.4 …"
    if curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | TAG=v5.7.4 bash; then
        ok "k3d installed"
    else
        BLOCKING+=("k3d|curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | TAG=v5.7.4 bash")
    fi
else
    ok "k3d: $(k3d version | head -1)"
fi

if ! command -v kubectl &>/dev/null; then
    info "kubectl not found — installing …"
    KVER=$(curl -Ls https://dl.k8s.io/release/stable.txt)
    if curl -sLo /tmp/kubectl "https://dl.k8s.io/release/${KVER}/bin/linux/amd64/kubectl" && \
       chmod +x /tmp/kubectl && sudo mv /tmp/kubectl /usr/local/bin/kubectl; then
        ok "kubectl installed"
    else
        BLOCKING+=("kubectl|curl -LO https://dl.k8s.io/release/\$(curl -Ls https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl && chmod +x kubectl && sudo mv kubectl /usr/local/bin/kubectl")
    fi
else
    ok "kubectl: $(kubectl version --client 2>/dev/null | grep 'Client' | head -1 || kubectl version --client --short 2>/dev/null | head -1 || echo 'installed')"
fi

# ── Show results ──────────────────────────────────────────────────────────────
if [[ ${#AUTO_FIXED[@]} -gt 0 ]]; then
    echo
    info "Auto-fixed ${#AUTO_FIXED[@]} issue(s):"
    for f in "${AUTO_FIXED[@]}"; do echo "    ✓ $f"; done
fi

if [[ ${#BLOCKING[@]} -gt 0 ]]; then
    echo
    echo "┌─────────────────────────────────────────────────────────────────┐"
    echo "│       Pre-flight FAILED — fix the issues below and re-run      │"
    echo "└─────────────────────────────────────────────────────────────────┘"
    n=1
    for entry in "${BLOCKING[@]}"; do
        label="${entry%%|*}"
        fix="${entry#*|}"
        echo
        echo -e "  \033[1;31m[$n]\033[0m $label"
        echo
        # Print each line of the fix command indented and highlighted
        while IFS= read -r line; do
            [[ -z "$line" ]] && continue
            echo -e "      \033[1;33m$\033[0m $line"
        done <<< "$fix"
        ((n++))
    done
    echo
    echo "  Re-run after fixing:  bash testenv/create.sh"
    echo
    exit 1
fi

ok "All pre-flight checks passed"

# ─── Generate credentials ─────────────────────────────────────────────────────

step "Credentials"

[[ -f "$ENV_FILE"   ]] && source "$ENV_FILE"   2>/dev/null || true
[[ -f "$USERS_FILE" ]] && source "$USERS_FILE" 2>/dev/null || true

if [[ -z "${POSTGRES_PASSWORD:-}" ]]; then
    POSTGRES_PASSWORD=$(openssl rand -hex 16)
    SONAR_ADMIN_PASSWORD=$(openssl rand -hex 12)
    JENKINS_ADMIN_USER="admin"
    JENKINS_ADMIN_PASSWORD=$(openssl rand -hex 12)
    GITEA_ADMIN_PASSWORD=$(openssl rand -hex 12)
    REGISTRY_PASSWORD=$(openssl rand -hex 12)
    info "Generated new credentials"
else
    ok "Re-using existing credentials from .env / .users"
fi

cat > "$ENV_FILE" <<ENVEOF
# AP3 Test Environment — $(date -Iseconds)
# ─────────────────────────────────────────────────────────────────────────────
# API tokens and service URLs.  DO NOT COMMIT.
# After create.sh completes:
#   set -a && source testenv/.env && set +a

SONARQUBE_TOKEN=${SONARQUBE_TOKEN:-__PENDING__}
JENKINS_USER=${JENKINS_ADMIN_USER:-admin}
JENKINS_TOKEN=${JENKINS_TOKEN:-__PENDING__}
GITEA_TOKEN=${GITEA_TOKEN:-__PENDING__}
# GITHUB_TOKEN is set to the Gitea token so platform scripts work without changes
GITHUB_TOKEN=${GITHUB_TOKEN:-__PENDING__}
K8S_API_URL=${K8S_API_URL:-__PENDING__}
K8S_SA_TOKEN=${K8S_SA_TOKEN:-__PENDING__}

# Service URLs (from the host)
JENKINS_URL=http://localhost:8080
SONARQUBE_URL=http://localhost:9000
GITEA_URL=http://localhost:3000
REGISTRY_URL=localhost:5000
# Org and shared-lib repo name (used by casc/jenkins.yaml via docker-compose)
GITEA_ORG=ap3
SHARED_LIB_REPO_NAME=jenkins-shared-lib
ENVEOF

cat > "$USERS_FILE" <<USERSEOF
# AP3 Test Environment — $(date -Iseconds)
# ─────────────────────────────────────────────────────────────────────────────
# Admin usernames and passwords for manual web UI access.  DO NOT COMMIT.
# Not needed by platform scripts — source testenv/.env for CLI/API usage.

POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
SONAR_ADMIN_PASSWORD=${SONAR_ADMIN_PASSWORD}
JENKINS_ADMIN_USER=${JENKINS_ADMIN_USER:-admin}
JENKINS_ADMIN_PASSWORD=${JENKINS_ADMIN_PASSWORD}
GITEA_ADMIN_USER=ap3admin
GITEA_ADMIN_PASSWORD=${GITEA_ADMIN_PASSWORD:-__PENDING__}
REGISTRY_PASSWORD=${REGISTRY_PASSWORD}
USERSEOF

ok ".env (tokens) and .users (passwords) written"
source "$ENV_FILE"
source "$USERS_FILE"

# Remove any stale backup files from old subnet-patching approach
rm -f "$SCRIPT_DIR/docker-compose.yml.bak" "$SCRIPT_DIR/jenkins/casc/jenkins.yaml.bak"

# ─── k3d cluster ──────────────────────────────────────────────────────────────

step "k3d cluster"

if k3d cluster list 2>/dev/null | grep -q "^${CLUSTER_NAME}[[:space:]]"; then
    ok "Cluster '$CLUSTER_NAME' already exists"
    k3d cluster start "$CLUSTER_NAME" 2>/dev/null || true
else
    info "Creating k3d cluster '$CLUSTER_NAME' (~60-90s) …"
    k3d cluster create "$CLUSTER_NAME" \
        --api-port  "0.0.0.0:${K3D_API_HOST_PORT}" \
        --k3s-arg   "--disable=traefik@server:0" \
        --wait \
        --timeout   180s
    ok "k3d cluster created"
fi

k3d kubeconfig merge "$CLUSTER_NAME" --kubeconfig-switch-context
ok "kubeconfig → k3d-${CLUSTER_NAME}"

# ─── k8s ServiceAccount ───────────────────────────────────────────────────────

step "Kubernetes ServiceAccount"

kubectl apply -f "$SCRIPT_DIR/k8s/jenkins-sa.yaml"
ok "SA resources applied"

# Create platform namespaces used for service deployments.
# In the testenv a single k3d cluster hosts all three environments, each
# isolated in its own namespace (platform-dev / platform-val / platform-prod).
step "Platform namespaces"
kubectl apply -f "$SCRIPT_DIR/k8s/platform-namespaces.yaml"
ok "platform-dev / platform-val / platform-prod ready"

info "Waiting for SA token …"
K8S_SA_TOKEN=""
for i in $(seq 1 20); do
    K8S_SA_TOKEN=$(kubectl -n "$BUILDS_NS" get secret jenkins-sa-token \
        -o jsonpath='{.data.token}' 2>/dev/null | base64 -d 2>/dev/null || echo "")
    [[ -n "$K8S_SA_TOKEN" ]] && break
    sleep 3
done
[[ -z "$K8S_SA_TOKEN" ]] && die "SA token never populated. Check: kubectl -n $BUILDS_NS describe secret jenkins-sa-token"

update_env "K8S_SA_TOKEN" "$K8S_SA_TOKEN"
ok "SA token retrieved"

# Jenkins joins the k3d-ap3 Podman network (declared in docker-compose.yml),
# so it can reach the k3d loadbalancer container directly at port 6443.
# This avoids host port-forwarding (host.containers.internal:6550) which is
# blocked by Netavark nft rules when Jenkins is on a different bridge network.
K8S_API_URL="https://k3d-${CLUSTER_NAME}-serverlb:6443"
update_env "K8S_API_URL" "$K8S_API_URL"
ok "k3d API (via k3d network): $K8S_API_URL"

source "$ENV_FILE"

# ─── Phase 1: Gitea + SonarQube + Registry ────────────────────────────────────

step "Gitea + SonarQube + Registry"

dc up -d postgres sonarqube registry gitea

# ── Gitea ─────────────────────────────────────────────────────────────────────

# Wait for Gitea web UI, then for the API to be truly ready (DB migrations done)
wait_http "http://localhost:3000" "Gitea HTTP" 30 3

info "Waiting for Gitea API to be ready …"
for _i in $(seq 1 40); do
    if curl -sf --max-time 3 "http://localhost:3000/api/v1/settings/api" >/dev/null 2>&1; then
        ok "Gitea API ready"; break
    fi
    printf "."; sleep 3
done
echo
if ! curl -sf --max-time 3 "http://localhost:3000/api/v1/settings/api" >/dev/null 2>&1; then
    die "Gitea API never became ready. Check: dc logs gitea"
fi

# Validate stored token — regenerate if invalid (e.g. after volume reset)
if [[ "${GITEA_TOKEN:-__PENDING__}" != "__PENDING__" ]]; then
    _tok_status=$(curl -sf -o /dev/null -w "%{http_code}" \
        -H "Authorization: token ${GITEA_TOKEN}" \
        "http://localhost:3000/api/v1/user" 2>/dev/null || echo "000")
    if [[ "$_tok_status" != "200" ]]; then
        warn "Stored Gitea token is stale (HTTP $_tok_status) — will regenerate"
        GITEA_TOKEN="__PENDING__"
        update_env "GITEA_TOKEN"  "__PENDING__"
        update_env "GITHUB_TOKEN" "__PENDING__"
    fi
fi

if [[ "${GITEA_TOKEN:-__PENDING__}" == "__PENDING__" ]]; then
    # Register admin via web form — first user Gitea auto-promotes to admin.
    # We avoid the gitea CLI because it writes to a different SQLite path than
    # the running server inside the container.
    info "Registering Gitea admin user 'ap3admin' via web form …"

    # Fetch signup page to get the CSRF token
    GITEA_CSRF=$(curl -sc /tmp/ap3_gitea_jar \
        --max-time 10 "http://localhost:3000/user/sign_up" 2>/dev/null \
        | grep -oP 'name="_csrf"\s+value="\K[^"]+' | head -1)

    if [[ -z "$GITEA_CSRF" ]]; then
        # Fallback: try meta tag format
        GITEA_CSRF=$(curl -sc /tmp/ap3_gitea_jar \
            --max-time 10 "http://localhost:3000/user/sign_up" 2>/dev/null \
            | grep -oP 'content="\K[^"]+(?="[^>]*name="_csrf")' | head -1)
    fi

    if [[ -n "$GITEA_CSRF" ]]; then
        SIGNUP_STATUS=$(curl -sb /tmp/ap3_gitea_jar -c /tmp/ap3_gitea_jar \
            -s --max-time 10 -o /dev/null -w "%{http_code}" \
            -X POST "http://localhost:3000/user/sign_up" \
            --data-urlencode "_csrf=$GITEA_CSRF" \
            --data-urlencode "user_name=ap3admin" \
            --data-urlencode "email=admin@ap3.local" \
            --data-urlencode "password=$GITEA_ADMIN_PASSWORD" \
            --data-urlencode "retype=$GITEA_ADMIN_PASSWORD" \
            -L 2>/dev/null || echo "000")
        info "  signup HTTP status: $SIGNUP_STATUS"
    else
        warn "Could not extract CSRF token from Gitea signup page"
    fi

    # Verify the user exists regardless of how it was created
    if ! curl -sf --max-time 5 \
            -u "ap3admin:${GITEA_ADMIN_PASSWORD}" \
            "http://localhost:3000/api/v1/user" >/dev/null 2>&1; then
        warn "ap3admin not reachable — dumping signup page for diagnosis:"
        curl -s --max-time 5 "http://localhost:3000/user/sign_up" 2>/dev/null | grep -i "csrf\|error\|flash" | head -10
        die "Could not create Gitea admin user. Check logs: dc logs gitea"
    fi
    ok "Gitea admin user 'ap3admin' ready"

    # Delete stale token if any, then create a fresh one
    curl -s --max-time 10 \
        -u "ap3admin:${GITEA_ADMIN_PASSWORD}" -X DELETE \
        "http://localhost:3000/api/v1/users/ap3admin/tokens/ap3-platform" \
        >/dev/null 2>&1 || true

    # Gitea 1.21+ requires explicit scopes on token creation.
    # We request broad write access so the platform CLI can manage repos, orgs, and users.
    HTTP_STATUS=""
    GITEA_TOKEN_RESP=$(curl -s --max-time 10 \
        -w "\n__STATUS__%{http_code}" \
        -u "ap3admin:${GITEA_ADMIN_PASSWORD}" \
        -X POST "http://localhost:3000/api/v1/users/ap3admin/tokens" \
        -H "Content-Type: application/json" \
        -d '{
          "name": "ap3-platform",
          "scopes": [
            "write:repository", "read:repository",
            "write:organization", "read:organization",
            "write:user", "read:user",
            "write:admin", "read:admin",
            "write:issue", "read:issue",
            "read:misc"
          ]
        }' 2>/dev/null || echo "")
    HTTP_STATUS=$(echo "$GITEA_TOKEN_RESP" | grep "__STATUS__" | sed 's/__STATUS__//')
    GITEA_TOKEN_BODY=$(echo "$GITEA_TOKEN_RESP" | grep -v "__STATUS__")
    GITEA_TOKEN=$(echo "$GITEA_TOKEN_BODY" \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['sha1'])" 2>/dev/null || echo "")
    if [[ -z "$GITEA_TOKEN" ]]; then
        warn "Gitea token request HTTP $HTTP_STATUS — body: $GITEA_TOKEN_BODY"
        die "Failed to create Gitea token"
    fi

    # Create org matching github_org in platform-test.yaml
    curl -sf --max-time 10 \
        -H "Authorization: token ${GITEA_TOKEN}" \
        -X POST "http://localhost:3000/api/v1/orgs" \
        -H "Content-Type: application/json" \
        -d '{"username":"ap3","visibility":"public","repo_admin_change_team_access":true}' \
        >/dev/null 2>&1 || info "  (org already exists)"

    update_users "GITEA_ADMIN_PASSWORD" "$GITEA_ADMIN_PASSWORD"
    update_env   "GITEA_TOKEN"          "$GITEA_TOKEN"
    update_env   "GITHUB_TOKEN"         "$GITEA_TOKEN"
    ok "Gitea configured  (ap3admin / ${GITEA_ADMIN_PASSWORD})"
else
    ok "Gitea token already captured"
fi

source "$ENV_FILE"

# ── SonarQube ─────────────────────────────────────────────────────────────────

wait_http "http://localhost:9000/api/system/status" "SonarQube HTTP" 80 5

info "Waiting for SonarQube UP status …"
SQ_STATUS=""
for i in $(seq 1 40); do
    SQ_STATUS=$(curl -sf "http://localhost:9000/api/system/status" \
        | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")
    [[ "$SQ_STATUS" == "UP" ]] && { ok "SonarQube: UP"; break; }
    printf "."; sleep 5
done
[[ "$SQ_STATUS" != "UP" ]] && die "SonarQube never reached UP. Check: dc logs sonarqube"

if curl -sf --max-time 10 -u "admin:admin" \
        "http://localhost:9000/api/authentication/validate" \
        2>/dev/null | grep -q '"valid":true'; then
    info "Changing SonarQube admin password …"
    curl -sf --max-time 10 -u "admin:admin" \
        -X POST "http://localhost:9000/api/users/change_password" \
        --data-urlencode "login=admin" \
        --data-urlencode "password=${SONAR_ADMIN_PASSWORD}" \
        --data-urlencode "previousPassword=admin" >/dev/null
    ok "SonarQube admin password changed"
fi

if [[ "${SONARQUBE_TOKEN:-__PENDING__}" == "__PENDING__" ]]; then
    curl -sf --max-time 10 -u "admin:${SONAR_ADMIN_PASSWORD}" -X POST \
        "http://localhost:9000/api/user_tokens/revoke" -d "name=jenkins-token" >/dev/null 2>&1 || true

    SQ_RESP=$(curl -sf --max-time 10 -u "admin:${SONAR_ADMIN_PASSWORD}" -X POST \
        "http://localhost:9000/api/user_tokens/generate" \
        -d "name=jenkins-token&type=GLOBAL_ANALYSIS_TOKEN")
    SONARQUBE_TOKEN=$(echo "$SQ_RESP" \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])" 2>/dev/null || echo "")
    [[ -z "$SONARQUBE_TOKEN" ]] && die "Failed to get SonarQube token. Response: $SQ_RESP"
    update_env "SONARQUBE_TOKEN" "$SONARQUBE_TOKEN"
    ok "SonarQube token generated"
fi

source "$ENV_FILE"

# ─── Gitea → k3d DNS registration ────────────────────────────────────────────
# k3d pods use CoreDNS which has no knowledge of Docker/Podman compose service
# names. Connect Gitea to the k3d network and inject its IP into the CoreDNS
# NodeHosts file so that 'gitea' resolves inside every agent pod.

step "Gitea k3d DNS"

docker network connect "k3d-${CLUSTER_NAME}" ap3-gitea 2>/dev/null && \
    ok "Gitea connected to k3d-${CLUSTER_NAME}" || \
    ok "Gitea already on k3d-${CLUSTER_NAME} network"

# Extract Gitea's IP on the k3d network by inspecting the network JSON directly.
# docker network inspect returns a Containers map keyed by container ID;
# we use python3 to find the entry whose Name matches ap3-gitea.
GITEA_K3D_IP=$(docker network inspect "k3d-${CLUSTER_NAME}" 2>/dev/null \
    | python3 -c "
import sys, json
data = json.load(sys.stdin)
containers = data[0].get('Containers', {}) if data else {}
for c in containers.values():
    if c.get('Name') == 'ap3-gitea':
        print(c.get('IPv4Address','').split('/')[0])
        break
" 2>/dev/null || true)

if [[ -z "$GITEA_K3D_IP" ]]; then
    warn "Could not determine Gitea IP on k3d-${CLUSTER_NAME} — CoreDNS patch skipped."
    warn "k3d pods may not resolve 'gitea'. Re-run: bash testenv/create.sh (idempotent)."
else
    ok "Gitea IP on k3d-${CLUSTER_NAME}: ${GITEA_K3D_IP}"

    # k3s CoreDNS serves the NodeHosts ConfigMap key as /etc/coredns/NodeHosts.
    # Adding 'gitea' there makes it resolve for all pods without restarting CoreDNS
    # (the hosts plugin reloads on a 15s interval by default).
    CURRENT_NODEHOSTS=$(kubectl -n kube-system get configmap coredns \
        -o jsonpath='{.data.NodeHosts}' 2>/dev/null || echo "")

    if echo "$CURRENT_NODEHOSTS" | grep -q " gitea$"; then
        ok "CoreDNS already has entry for gitea"
    else
        NEW_NODEHOSTS="${CURRENT_NODEHOSTS}
${GITEA_K3D_IP} gitea"
        kubectl -n kube-system patch configmap coredns --type=merge \
            -p "{\"data\":{\"NodeHosts\":$(echo "$NEW_NODEHOSTS" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))')}}"
        ok "CoreDNS patched: gitea → ${GITEA_K3D_IP}"
    fi
fi

# ─── Phase 2: Jenkins ─────────────────────────────────────────────────────────

step "Jenkins"

info "Building Jenkins image (plugin downloads ~5-10 min on first run) …"
dc build jenkins

dc up -d jenkins

# Wait for Jenkins port to open
wait_http "http://localhost:8080/login" "Jenkins HTTP" 60 5

# Wait for Jenkins to finish JCasC initialisation — the crumb endpoint
# only responds once the security realm is fully loaded.
info "Waiting for Jenkins to finish initialising (JCasC) …"
CRUMB_JSON=""
for attempt in $(seq 1 30); do
    CRUMB_JSON=$(curl -sf -u "${JENKINS_ADMIN_USER}:${JENKINS_ADMIN_PASSWORD}" \
        "http://localhost:8080/crumbIssuer/api/json" 2>/dev/null || echo "")
    [[ -n "$CRUMB_JSON" ]] && { ok "Jenkins: fully initialised"; break; }
    printf "."; sleep 6
done
echo

if [[ -z "$CRUMB_JSON" ]]; then
    warn "Jenkins did not initialise within 3 min. Showing container logs:"
    dc logs --tail=40 jenkins 2>/dev/null || true
    die "Jenkins not ready. Fix the issue above and re-run."
fi

info "Generating Jenkins API token …"
JENKINS_API_TOKEN=""
JENKINS_JAR="/tmp/ap3_jenkins_jar"
TOKEN_URL="http://localhost:8080/user/${JENKINS_ADMIN_USER}/descriptorByName/jenkins.security.ApiTokenProperty/generateNewToken"

# Jenkins crumbs are session-scoped. We must fetch the crumb and POST the token
# request within the SAME session (same cookie jar). Using basic auth alone creates
# a new session for every request, making the crumb invalid by the time we POST.
#
# Also: Jenkins sometimes restarts after JCasC finishes (security realm init).
# We loop with a fresh cookie jar each attempt to handle that gracefully.

jenkins_generate_token() {
    rm -f "$JENKINS_JAR"
    local crumb_json
    crumb_json=$(curl -sf --max-time 15 \
        -c "$JENKINS_JAR" -b "$JENKINS_JAR" \
        -u "${JENKINS_ADMIN_USER}:${JENKINS_ADMIN_PASSWORD}" \
        "http://localhost:8080/crumbIssuer/api/json" 2>/dev/null || echo "")
    [[ -z "$crumb_json" ]] && return 1

    local cf cv
    cf=$(echo "$crumb_json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['crumbRequestField'])" 2>/dev/null || echo "")
    cv=$(echo "$crumb_json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['crumb'])" 2>/dev/null || echo "")
    [[ -z "$cf" || -z "$cv" ]] && return 1

    info "  crumb field=$cf value=${cv:0:12}…"
    local tr_raw tr_status tr
    tr_raw=$(curl -s --max-time 15 \
        -w "\n__HTTP_STATUS__%{http_code}" \
        -c "$JENKINS_JAR" -b "$JENKINS_JAR" \
        -u "${JENKINS_ADMIN_USER}:${JENKINS_ADMIN_PASSWORD}" \
        -H "${cf}: ${cv}" \
        -H "Content-Type: application/x-www-form-urlencoded" \
        -X POST "$TOKEN_URL" \
        --data-urlencode "newTokenName=ap3-cli-token" 2>/dev/null || echo "")
    tr_status=$(echo "$tr_raw" | grep "__HTTP_STATUS__" | sed 's/__HTTP_STATUS__//')
    tr=$(echo "$tr_raw" | grep -v "__HTTP_STATUS__")
    info "  token gen HTTP $tr_status — response: ${tr:0:120}"

    JENKINS_API_TOKEN=$(echo "$tr" \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['tokenValue'])" 2>/dev/null || echo "")
    [[ -n "$JENKINS_API_TOKEN" ]]
}

# Try immediately (crumb endpoint already responded above), then wait for
# any JCasC-triggered restart and retry.
for _attempt in 1 2 3; do
    if jenkins_generate_token; then
        break
    fi
    info "  Jenkins not ready for token gen — waiting for post-JCasC restart …"
    sleep 15
    wait_http "http://localhost:8080/login" "Jenkins (post-restart)" 30 5
    # Wait for crumb endpoint to be responsive again
    for _i in $(seq 1 20); do
        curl -sf --max-time 5 \
            -u "${JENKINS_ADMIN_USER}:${JENKINS_ADMIN_PASSWORD}" \
            "http://localhost:8080/crumbIssuer/api/json" >/dev/null 2>&1 && break
        printf "."; sleep 5
    done
    echo
done

if [[ -n "$JENKINS_API_TOKEN" ]]; then
    update_env "JENKINS_USER"   "$JENKINS_ADMIN_USER"
    update_env "JENKINS_TOKEN"  "$JENKINS_API_TOKEN"
    ok "Jenkins API token generated"
else
    warn "Could not auto-generate Jenkins API token."
    warn "Do it manually: Jenkins → ${JENKINS_ADMIN_USER} → Configure → API Token → Add new token"
fi

source "$ENV_FILE"

# ─── Generate bootstrap-config.yaml ──────────────────────────────────────────
# All tokens are now live in the environment. Write testenv-specific answers for
# every wizard prompt so bootstrap.sh can run non-interactively.
# ${K8S_API_URL} is substituted by the shell (unquoted heredoc delimiter).
# All other values are literals — no shell expansion needed.

step "Generating testenv/bootstrap-config.yaml"

cat > "${SCRIPT_DIR}/bootstrap-config.yaml" <<BSCFG
# bootstrap-config.yaml — Pre-answered wizard config for the AP3 testenv.
# Generated by testenv/create.sh on $(date -Iseconds).
# Usage:
#   set -a && source testenv/.env && set +a
#   ./bootstrap/bootstrap.sh --config testenv/bootstrap-config.yaml

# ── Platform instance location ───────────────────────────────────────────────
platform_target_dir: "../platform-instance"

# ── Git hosting (Gitea) ──────────────────────────────────────────────────────
github_url: "http://localhost:3000"
github_api_path: "api/v1"
github_account_type: "org"
github_org: "ap3"
platform_repo_name: "platform"
shared_lib_repo_name: "jenkins-shared-lib"

# ── Jenkins ──────────────────────────────────────────────────────────────────
jenkins_url: "http://localhost:8080"
# Jenkins container reaches Gitea via the Docker service hostname, not localhost.
jenkins_git_url: "http://gitea:3000"
# Gitea container reaches Jenkins via the Docker service hostname for webhook delivery.
jenkins_hook_url: "http://jenkins:8080"

# ── Platform defaults ────────────────────────────────────────────────────────
platform: "openshift"
cluster_prefix: "openshift"

# ── Standard environments — all mapped to the local k3d cluster ──────────────
environments:
  prod:
    name: "prod"
    cluster: "openshift-prod"
    api_url: "${K8S_API_URL}"
    context: "k3d-ap3"
    registry: "localhost:5000"
    namespace: "platform-prod"
  val:
    name: "val"
    cluster: "openshift-val"
    api_url: "${K8S_API_URL}"
    context: "k3d-ap3"
    registry: "localhost:5000"
    namespace: "platform-val"
  dev:
    name: "dev"
    cluster: "openshift-dev"
    api_url: "${K8S_API_URL}"
    context: "k3d-ap3"
    registry: "localhost:5000"
    namespace: "platform-dev"
BSCFG

ok "testenv/bootstrap-config.yaml written  (K8S_API_URL=${K8S_API_URL})"

# ─── Service health checks ────────────────────────────────────────────────────

step "Service health checks"

svc_check() {
    local label="$1" url="$2" pattern="${3:-}"
    local out
    out=$(curl -sf --max-time 5 "$url" 2>/dev/null || echo "")
    if [[ -z "$pattern" && -n "$out" ]] || [[ -n "$pattern" && "$out" == *"$pattern"* ]]; then
        ok "$label  →  $url"
    else
        warn "$label  NOT reachable at $url"
        info "  Check: curl -v $url"
    fi
}

svc_check "Gitea"     "http://localhost:3000"                       "Gitea"
svc_check "SonarQube" "http://localhost:9000/api/system/status"     '"status":"UP"'
svc_check "Registry"  "http://localhost:5000/v2/"                   ""
svc_check "Jenkins"   "http://localhost:8080/login"                 "Jenkins"
if kubectl cluster-info --context "k3d-${CLUSTER_NAME}" >/dev/null 2>&1; then
    ok "k3d API  →  https://localhost:${K3D_API_HOST_PORT}  (kubectl OK)"
else
    warn "k3d API not responding — check: kubectl cluster-info --context k3d-${CLUSTER_NAME}"
fi

# ─── Summary ──────────────────────────────────────────────────────────────────

echo
echo "╔══════════════════════════════════════════════════════════════════════╗"
echo "║          AP3 Local Test Environment — READY                         ║"
echo "╠══════════════════════════════════════════════════════════════════════╣"
echo "║                                                                      ║"
printf "║  %-24s %-45s ║\n" "Gitea (Git hosting)"   "http://localhost:3000"
printf "║  %-24s %-45s ║\n" "  Admin user"          "ap3admin"
printf "║  %-24s %-45s ║\n" "  Admin password"      "see testenv/.users"
printf "║  %-24s %-45s ║\n" "  API token"           "${GITEA_TOKEN:-see .env}"
printf "║  %-24s %-45s ║\n" "  Org"                 "http://localhost:3000/ap3"
echo "║                                                                      ║"
printf "║  %-24s %-45s ║\n" "Jenkins (CI/CD)"       "http://localhost:8080"
printf "║  %-24s %-45s ║\n" "  Admin user"          "${JENKINS_ADMIN_USER}"
printf "║  %-24s %-45s ║\n" "  Admin password"      "see testenv/.users"
printf "║  %-24s %-45s ║\n" "  API token"           "${JENKINS_TOKEN:-not generated — see above}"
echo "║                                                                      ║"
printf "║  %-24s %-45s ║\n" "SonarQube (quality)"   "http://localhost:9000"
printf "║  %-24s %-45s ║\n" "  Admin user"          "admin"
printf "║  %-24s %-45s ║\n" "  Admin password"      "see testenv/.users"
printf "║  %-24s %-45s ║\n" "  Analysis token"      "${SONARQUBE_TOKEN:-see .env}"
echo "║                                                                      ║"
printf "║  %-24s %-45s ║\n" "Registry"              "localhost:5000  (no auth in test env)"
printf "║  %-24s %-45s ║\n" "k3d API"               "https://localhost:${K3D_API_HOST_PORT}"
echo "║                                                                      ║"
echo "╚══════════════════════════════════════════════════════════════════════╝"
echo
echo "  API tokens → testenv/.env   (source for CLI/bootstrap usage)"
echo "  Passwords  → testenv/.users (for manual web UI access)"
echo
echo "Credentials → testenv/.env (tokens)  testenv/.users (passwords)"
echo
echo "NEXT STEPS:"
echo "  1. Activate credentials (set -a exports all vars to subprocesses):"
echo "       set -a && source testenv/.env && set +a"
echo "  2. Bootstrap the platform (creates environments, repo, shared library):"
echo "       ./bootstrap/bootstrap.sh --config testenv/bootstrap-config.yaml"
echo "  3. Test: create a service and watch Jenkins pick it up:"
echo "       cd ../platform-instance && ./platform.sh svc create my-test-svc me --template springboot"
echo "       kubectl -n jenkins-builds get pods -w"
echo "  4. Browse Gitea:  http://localhost:3000/ap3"
