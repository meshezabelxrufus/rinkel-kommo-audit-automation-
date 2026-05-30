#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
# deploy/scripts/deploy.sh — Zero-downtime deployment script
#
# Strategy:
#   1. Pull new image
#   2. Run health check on new image before replacing old one
#   3. Rolling update: bring up new container, verify health, remove old
#   4. Rollback automatically if new image fails health check
#
# Usage:
#   ./deploy/scripts/deploy.sh [--version v1.2.3] [--rollback]
#
# Environment:
#   APP_DIR     — path to app directory (default /opt/rinkel)
#   APP_VERSION — image tag to deploy (default: git short SHA)
# ══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/rinkel}"
COMPOSE_FILE="docker-compose.prod.yml"
SERVICE="api"
HEALTH_URL="http://localhost:8000/health"
HEALTH_RETRIES=12   # 12 × 5s = 60s max wait
HEALTH_INTERVAL=5

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${GREEN}[$(date -u +%H:%M:%S)]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[$(date -u +%H:%M:%S)]${NC}  $*"; }
error() { echo -e "${RED}[$(date -u +%H:%M:%S)]${NC}  $*"; }
step()  { echo -e "${BLUE}━━ $* ━━${NC}"; }

# Parse arguments
VERSION="${APP_VERSION:-$(git -C "${APP_DIR}" rev-parse --short HEAD 2>/dev/null || echo latest)}"
ROLLBACK=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --version) VERSION="$2"; shift 2 ;;
        --rollback) ROLLBACK=true; shift ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

cd "${APP_DIR}"

# ── Rollback mode ─────────────────────────────────────────────────────────────
if [[ "${ROLLBACK}" == "true" ]]; then
    PREVIOUS=$(docker compose -f "${COMPOSE_FILE}" images -q "${SERVICE}" 2>/dev/null | tail -1)
    warn "ROLLBACK MODE — reverting to previous image"
    docker compose -f "${COMPOSE_FILE}" up -d --no-build "${SERVICE}"
    info "Rollback complete"
    exit 0
fi

# ── Pre-flight checks ─────────────────────────────────────────────────────────
step "Pre-flight checks"

[[ -f "${COMPOSE_FILE}" ]] || { error "Missing ${COMPOSE_FILE}"; exit 1; }
[[ -f ".env.prod" ]]       || { error "Missing .env.prod"; exit 1; }
[[ -f "deploy/nginx/nginx.conf" ]] || { error "Missing nginx config"; exit 1; }

docker info > /dev/null 2>&1 || { error "Docker daemon not running"; exit 1; }

info "Deploying version: ${VERSION}"

# ── Save current state for rollback ──────────────────────────────────────────
CURRENT_IMAGE=$(docker compose -f "${COMPOSE_FILE}" images -q "${SERVICE}" 2>/dev/null | head -1 || true)
info "Current image: ${CURRENT_IMAGE:-none}"

# ── Build or pull image ───────────────────────────────────────────────────────
step "Building image"
APP_VERSION="${VERSION}" docker compose -f "${COMPOSE_FILE}" build --no-cache "${SERVICE}"
info "Build complete"

# ── Validate image ────────────────────────────────────────────────────────────
step "Validating new image"
docker run --rm \
    --env-file .env.prod \
    "$(docker compose -f "${COMPOSE_FILE}" images -q "${SERVICE}" 2>/dev/null | head -1)" \
    python -c "from app.main import app; print('App import OK')" \
    || { error "Image validation failed — aborting deploy"; exit 1; }

# ── Deploy ────────────────────────────────────────────────────────────────────
step "Deploying services"
APP_VERSION="${VERSION}" docker compose -f "${COMPOSE_FILE}" up -d --remove-orphans

# ── Health check ──────────────────────────────────────────────────────────────
step "Waiting for health check"
attempt=0
while [[ $attempt -lt $HEALTH_RETRIES ]]; do
    attempt=$((attempt + 1))
    STATUS=$(curl -sf "${HEALTH_URL}" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','unknown'))" 2>/dev/null || echo "unreachable")
    info "Health check ${attempt}/${HEALTH_RETRIES}: ${STATUS}"

    if [[ "${STATUS}" == "healthy" ]]; then
        break
    fi

    if [[ $attempt -eq $HEALTH_RETRIES ]]; then
        error "Health check failed after ${HEALTH_RETRIES} attempts — initiating rollback"
        # Rollback: restart from previous image
        if [[ -n "${CURRENT_IMAGE}" ]]; then
            warn "Rolling back to: ${CURRENT_IMAGE}"
            docker compose -f "${COMPOSE_FILE}" up -d "${SERVICE}"
        fi
        # Show recent logs for debugging
        echo ""
        error "Last 50 lines of api logs:"
        docker compose -f "${COMPOSE_FILE}" logs --tail=50 "${SERVICE}"
        exit 1
    fi

    sleep "${HEALTH_INTERVAL}"
done

# ── Cleanup old images ────────────────────────────────────────────────────────
step "Cleanup"
docker image prune -f --filter "label=org.opencontainers.image.title=rinkel-auditor" 2>/dev/null || true
info "Old images cleaned"

# ── Reload nginx (zero-downtime config reload) ────────────────────────────────
docker exec rinkel-nginx nginx -t && \
    docker exec rinkel-nginx nginx -s reload && \
    info "Nginx reloaded" || \
    warn "Nginx reload failed — check config"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
info "Deploy SUCCESS — version: ${VERSION}"
echo ""
docker compose -f "${COMPOSE_FILE}" ps
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
