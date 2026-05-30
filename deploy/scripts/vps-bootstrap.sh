#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
# deploy/scripts/vps-bootstrap.sh
#
# ONE-TIME VPS bootstrap for Ubuntu 22.04 / 24.04 LTS.
# Run as root (or sudo) on a fresh VPS.
#
# What it does:
#   1. System update + hardening packages
#   2. Firewall (UFW) setup
#   3. Docker CE + Docker Compose plugin
#   4. Non-root deploy user
#   5. Directory structure + permissions
#   6. Certbot for Let's Encrypt SSL
#   7. Logrotate for persistent logs
#   8. Fail2Ban for SSH + Nginx brute-force protection
#
# Usage:
#   curl -fsSL https://your-host/vps-bootstrap.sh | sudo bash
#   # OR copy to VPS and run:
#   chmod +x vps-bootstrap.sh && sudo ./vps-bootstrap.sh
# ══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

DEPLOY_USER="rinkel"
APP_DIR="/opt/rinkel"
DATA_DIR="/var/rinkel"
LOG_DIR="/var/log/rinkel"
DOMAIN="${DOMAIN:-api.yourdomain.nl}"
EMAIL="${EMAIL:-devops@company.nl}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

[[ $EUID -ne 0 ]] && error "Run as root: sudo $0"

# ── 1. System update ──────────────────────────────────────────────────────────
info "Updating system packages..."
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -qq
apt-get install -y -qq \
    curl \
    wget \
    git \
    htop \
    unzip \
    ca-certificates \
    gnupg \
    lsb-release \
    ufw \
    fail2ban \
    logrotate \
    cron \
    openssl

# ── 2. Firewall (UFW) ─────────────────────────────────────────────────────────
info "Configuring UFW firewall..."
ufw --force disable   # reset first
ufw default deny incoming
ufw default allow outgoing
# SSH — CHANGE PORT if you use non-standard SSH
ufw allow 22/tcp comment "SSH"
# HTTP + HTTPS for Nginx
ufw allow 80/tcp comment "HTTP"
ufw allow 443/tcp comment "HTTPS"
# Block direct access to app port (nginx proxies it)
# Port 8000 intentionally NOT opened
ufw --force enable
info "UFW status:"
ufw status verbose

# ── 3. SSH hardening ──────────────────────────────────────────────────────────
info "Hardening SSH..."
# Disable password auth (assumes you have SSH keys configured)
sed -i 's/#PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/#PermitRootLogin yes/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
systemctl reload sshd

# ── 4. Fail2Ban ──────────────────────────────────────────────────────────────
info "Configuring Fail2Ban..."
cat > /etc/fail2ban/jail.local << 'EOF'
[DEFAULT]
bantime  = 1h
findtime = 10m
maxretry = 5
backend  = systemd

[sshd]
enabled = true
port    = ssh
filter  = sshd
logpath = /var/log/auth.log
maxretry = 3

[nginx-http-auth]
enabled = true
filter  = nginx-http-auth
logpath = /var/log/nginx/rinkel/error.log

[nginx-limit-req]
enabled  = true
filter   = nginx-limit-req
logpath  = /var/log/nginx/rinkel/error.log
maxretry = 10
EOF
systemctl enable fail2ban
systemctl restart fail2ban

# ── 5. Docker CE ─────────────────────────────────────────────────────────────
info "Installing Docker CE..."
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
    gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/ubuntu \
    $(lsb_release -cs) stable" | \
    tee /etc/apt/sources.list.d/docker.list > /dev/null

apt-get update -qq
apt-get install -y -qq \
    docker-ce \
    docker-ce-cli \
    containerd.io \
    docker-buildx-plugin \
    docker-compose-plugin

systemctl enable docker
systemctl start docker
info "Docker version: $(docker --version)"
info "Compose version: $(docker compose version)"

# ── 6. Deploy user ────────────────────────────────────────────────────────────
info "Creating deploy user: ${DEPLOY_USER}..."
if id "${DEPLOY_USER}" &>/dev/null; then
    warn "User ${DEPLOY_USER} already exists — skipping creation"
else
    useradd \
        --system \
        --create-home \
        --home-dir /home/${DEPLOY_USER} \
        --shell /bin/bash \
        --groups docker \
        "${DEPLOY_USER}"
    info "Created user ${DEPLOY_USER} with docker group"
fi

# ── 7. Directory structure ────────────────────────────────────────────────────
info "Creating directory structure..."
mkdir -p \
    "${APP_DIR}" \
    "${DATA_DIR}/exports" \
    "${DATA_DIR}/credentials" \
    "${LOG_DIR}" \
    "/var/log/nginx/rinkel" \
    "/var/www/certbot"

chown -R "${DEPLOY_USER}:${DEPLOY_USER}" "${APP_DIR}"
chown -R "${DEPLOY_USER}:${DEPLOY_USER}" "${DATA_DIR}"
chown -R "${DEPLOY_USER}:${DEPLOY_USER}" "${LOG_DIR}"

# Secure credentials directory
chmod 700 "${DATA_DIR}/credentials"

# ── 8. Logrotate ─────────────────────────────────────────────────────────────
info "Configuring logrotate..."
cat > /etc/logrotate.d/rinkel << 'EOF'
/var/log/rinkel/*.log /var/log/nginx/rinkel/*.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    sharedscripts
    postrotate
        docker exec rinkel-nginx nginx -s reopen 2>/dev/null || true
    endscript
}
EOF

# ── 9. Let's Encrypt / Certbot ───────────────────────────────────────────────
info "Installing Certbot..."
apt-get install -y -qq certbot python3-certbot-nginx

info "Requesting SSL certificate for ${DOMAIN}..."
warn "Make sure DNS for ${DOMAIN} points to this server's IP before continuing!"
read -p "Press ENTER to request certificate, or Ctrl+C to skip and do it manually: "

certbot certonly \
    --standalone \
    --non-interactive \
    --agree-tos \
    --email "${EMAIL}" \
    -d "${DOMAIN}" \
    || warn "Certbot failed — configure SSL manually after DNS propagates"

# Auto-renewal cron
echo "0 3 * * * root certbot renew --quiet --post-hook 'docker exec rinkel-nginx nginx -s reload'" \
    > /etc/cron.d/certbot-renew

# ── 10. Cron jobs for pipeline maintenance ───────────────────────────────────
info "Setting up pipeline maintenance crons..."
cat > /etc/cron.d/rinkel-pipeline << 'EOF'
# Rinkel call auditor — pipeline maintenance
# m  h  dom  mon  dow  user      command

# Process any pending calls missed by real-time webhook (every 5 min)
*/5  *  *    *    *    rinkel    docker exec rinkel-api python -c "import asyncio; from app.workers.pipeline import process_pending_calls; asyncio.run(process_pending_calls())" >> /var/log/rinkel/cron.log 2>&1

# Process pending transcriptions (every 5 min)
*/5  *  *    *    *    rinkel    docker exec rinkel-api python -c "import asyncio; from app.workers.pipeline import process_pending_transcriptions; asyncio.run(process_pending_transcriptions())" >> /var/log/rinkel/cron.log 2>&1

# Retry failed audio downloads/uploads (every 15 min)
*/15 *  *    *    *    rinkel    docker exec rinkel-api python -c "import asyncio; from app.workers.pipeline import retry_failed_audio; asyncio.run(retry_failed_audio())" >> /var/log/rinkel/cron.log 2>&1

# Retry failed transcriptions (every 15 min)
*/15 *  *    *    *    rinkel    docker exec rinkel-api python -c "import asyncio; from app.workers.pipeline import retry_failed_transcriptions; asyncio.run(retry_failed_transcriptions())" >> /var/log/rinkel/cron.log 2>&1

# Clean stale temp audio files (daily 03:00)
0    3  *    *    *    rinkel    docker exec rinkel-api python -c "import asyncio; from app.workers.pipeline import cleanup_stale_files; asyncio.run(cleanup_stale_files())" >> /var/log/rinkel/cron.log 2>&1

# Clean old export files (daily 03:30)
30   3  *    *    *    rinkel    docker exec rinkel-api python -c "import asyncio; from app.services.export_service import ExportService; asyncio.run(ExportService().cleanup_old_exports())" >> /var/log/rinkel/cron.log 2>&1
EOF

info "VPS bootstrap complete!"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Next steps:"
echo "  1. Copy your app to ${APP_DIR}"
echo "     git clone https://github.com/your-org/rinkel.git ${APP_DIR}"
echo ""
echo "  2. Copy .env.prod (from .env.prod.example) to ${APP_DIR}/.env.prod"
echo "     chmod 600 ${APP_DIR}/.env.prod"
echo ""
echo "  3. Copy Google service account JSON:"
echo "     cp service-account.json ${DATA_DIR}/credentials/"
echo "     chmod 400 ${DATA_DIR}/credentials/service-account.json"
echo ""
echo "  4. Deploy:"
echo "     cd ${APP_DIR}"
echo "     docker compose -f docker-compose.prod.yml pull"
echo "     docker compose -f docker-compose.prod.yml up -d"
echo ""
echo "  5. Verify:"
echo "     curl https://${DOMAIN}/health"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
