#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
# deploy/scripts/backup.sh — Production backup script
#
# Backs up:
#   - PostgreSQL database dump (if running local postgres container)
#   - Export files (/var/rinkel/exports)
#   - Environment config (.env.prod — encrypted)
#
# Storage options (configure one):
#   - Local: /var/backups/rinkel/
#   - Remote: rclone to S3/B2/GCS (recommended for production)
#
# Schedule: add to crontab as:
#   0 2 * * * /opt/rinkel/deploy/scripts/backup.sh >> /var/log/rinkel/backup.log 2>&1
# ══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/rinkel}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/rinkel}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
TIMESTAMP=$(date -u +"%Y%m%d_%H%M%S")
BACKUP_NAME="rinkel_backup_${TIMESTAMP}"
BACKUP_PATH="${BACKUP_DIR}/${BACKUP_NAME}"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[$(date -u +%H:%M:%S)]${NC} $*"; }
warn()  { echo -e "${YELLOW}[$(date -u +%H:%M:%S)]${NC} $*"; }
error() { echo -e "${RED}[$(date -u +%H:%M:%S)]${NC} $*"; exit 1; }

mkdir -p "${BACKUP_PATH}"
info "Starting backup: ${BACKUP_NAME}"

# ── Database backup ───────────────────────────────────────────────────────────
if docker ps --format '{{.Names}}' | grep -q "rinkel-db"; then
    info "Dumping PostgreSQL database..."
    docker exec rinkel-db pg_dump \
        -U "${POSTGRES_USER:-rinkel_user}" \
        -d "${POSTGRES_DB:-rinkel}" \
        --no-owner \
        --no-privileges \
        --format=custom \
        --compress=9 \
        > "${BACKUP_PATH}/database.dump"

    SIZE=$(du -sh "${BACKUP_PATH}/database.dump" | cut -f1)
    info "Database dump complete: ${SIZE}"
else
    warn "Local postgres container not running — skipping DB backup (using Supabase cloud)"
    # For Supabase: use their dashboard backup or pg_dump against the Supabase connection string
fi

# ── Export files backup ────────────────────────────────────────────────────────
EXPORT_DIR="${EXPORT_HOST_DIR:-/var/rinkel/exports}"
if [[ -d "${EXPORT_DIR}" && -n "$(ls -A ${EXPORT_DIR} 2>/dev/null)" ]]; then
    info "Archiving export files..."
    tar -czf "${BACKUP_PATH}/exports.tar.gz" -C "$(dirname ${EXPORT_DIR})" "$(basename ${EXPORT_DIR})"
    SIZE=$(du -sh "${BACKUP_PATH}/exports.tar.gz" | cut -f1)
    info "Exports archived: ${SIZE}"
else
    info "No export files to back up"
fi

# ── Environment config backup (encrypted) ────────────────────────────────────
ENV_FILE="${APP_DIR}/.env.prod"
if [[ -f "${ENV_FILE}" ]]; then
    # Encrypt with openssl AES-256 — set BACKUP_ENCRYPTION_KEY in environment
    if [[ -n "${BACKUP_ENCRYPTION_KEY:-}" ]]; then
        openssl enc -aes-256-cbc -pbkdf2 -iter 100000 \
            -pass "pass:${BACKUP_ENCRYPTION_KEY}" \
            -in "${ENV_FILE}" \
            -out "${BACKUP_PATH}/env.enc"
        info "Environment config backed up (encrypted)"
    else
        warn "BACKUP_ENCRYPTION_KEY not set — skipping env backup"
    fi
fi

# ── Compress full backup ───────────────────────────────────────────────────────
info "Compressing full backup..."
cd "${BACKUP_DIR}"
tar -czf "${BACKUP_NAME}.tar.gz" "${BACKUP_NAME}/"
rm -rf "${BACKUP_PATH}"

FINAL_SIZE=$(du -sh "${BACKUP_DIR}/${BACKUP_NAME}.tar.gz" | cut -f1)
info "Final backup: ${BACKUP_DIR}/${BACKUP_NAME}.tar.gz (${FINAL_SIZE})"

# ── Upload to remote (optional — requires rclone configured) ──────────────────
if command -v rclone &> /dev/null && [[ -n "${RCLONE_REMOTE:-}" ]]; then
    info "Uploading to remote: ${RCLONE_REMOTE}..."
    rclone copy \
        "${BACKUP_DIR}/${BACKUP_NAME}.tar.gz" \
        "${RCLONE_REMOTE}/rinkel/backups/" \
        --progress
    info "Remote upload complete"
fi

# ── Retention: delete backups older than RETENTION_DAYS ───────────────────────
info "Cleaning backups older than ${RETENTION_DAYS} days..."
find "${BACKUP_DIR}" -name "rinkel_backup_*.tar.gz" -mtime "+${RETENTION_DAYS}" -delete
REMAINING=$(find "${BACKUP_DIR}" -name "rinkel_backup_*.tar.gz" | wc -l)
info "Backup retention: ${REMAINING} backups kept"

info "Backup complete: ${BACKUP_NAME}"
