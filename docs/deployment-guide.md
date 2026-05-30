# Rinkel Call Auditor — Production Deployment Guide

## Architecture Overview

```
                                Internet
                                    │
                          ┌─────────▼──────────┐
                          │   UFW Firewall       │
                          │  (80, 443, 22 only)  │
                          └─────────┬──────────┘
                                    │
                          ┌─────────▼──────────┐
                          │     Nginx 1.27      │
                          │  SSL termination    │
                          │  Rate limiting      │
                          │  Security headers   │
                          └─────────┬──────────┘
                                    │ proxy_pass :8000
                          ┌─────────▼──────────┐
                          │   FastAPI (uvicorn) │
                          │   2 workers         │
                          │   container: api    │
                          └──────┬──────┬───────┘
                                 │      │
               ┌─────────────────┘      └──────────────────┐
               │                                           │
    ┌──────────▼──────────┐                   ┌────────────▼────────────┐
    │  Supabase / Postgres │                   │    External APIs         │
    │  (DATABASE_URL)      │                   │  Google Drive            │
    └──────────────────────┘                   │  OpenAI Whisper          │
                                               └─────────────────────────┘
```

> [!IMPORTANT]
> Port 8000 (FastAPI) is **never** opened in the firewall. All traffic enters through Nginx on 443/80. This is enforced by UFW and the docker-compose `expose` vs `ports` distinction.

---

## Recommended VPS Specs

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| CPU | 2 vCPU | 4 vCPU |
| RAM | 2 GB | 4 GB |
| Disk | 40 GB SSD | 80 GB SSD |
| OS | Ubuntu 22.04 LTS | Ubuntu 24.04 LTS |
| Bandwidth | 1 TB/mo | 2 TB/mo |

**Providers**: Hetzner CX22 (€4.51/mo), DigitalOcean Droplet, Linode Shared 4GB

---

## Phase 1 — VPS Initial Setup

### 1.1 First Login

```bash
# From your local machine — replace with your VPS IP
ssh root@YOUR_VPS_IP

# Verify OS
lsb_release -a
```

### 1.2 Run Bootstrap Script

Copy the bootstrap script to the server and execute it:

```bash
# On your local machine
scp deploy/scripts/vps-bootstrap.sh root@YOUR_VPS_IP:/tmp/

# On the VPS
DOMAIN="api.yourdomain.nl" \
EMAIL="devops@company.nl" \
bash /tmp/vps-bootstrap.sh
```

The bootstrap script handles:
- ✅ System update
- ✅ UFW firewall (22, 80, 443 only)
- ✅ SSH hardening (password auth disabled)
- ✅ Fail2Ban (SSH + Nginx brute-force protection)
- ✅ Docker CE + Compose plugin
- ✅ `rinkel` deploy user with docker group
- ✅ Directory structure
- ✅ Logrotate
- ✅ Certbot + Let's Encrypt certificate
- ✅ Pipeline maintenance crons

### 1.3 Verify Post-Bootstrap

```bash
# Firewall
ufw status verbose

# Docker
docker --version
docker compose version

# Users
id rinkel

# Directories
ls -la /var/rinkel/
ls -la /opt/rinkel/
```

---

## Phase 2 — Application Setup

### 2.1 Clone Repository

```bash
# Switch to deploy user
su - rinkel

# Clone to application directory
git clone https://github.com/your-org/rinkel.git /opt/rinkel
cd /opt/rinkel
```

### 2.2 Configure Environment

```bash
# Copy the example env to production env
cp .env.prod.example .env.prod

# Edit with your real values
nano .env.prod

# Secure the file — CRITICAL
chmod 600 .env.prod
chown rinkel:rinkel .env.prod
```

Key variables to fill in `.env.prod`:

```bash
# Required — get from Supabase dashboard
SUPABASE_URL=https://your-project-ref.supabase.co
SUPABASE_ANON_KEY=eyJ...
DATABASE_URL=postgresql+asyncpg://postgres.[ref]:[pass]@aws-0-eu-central-1.pooler.supabase.com:5432/postgres

# Required — generate a strong secret
RINKEL_WEBHOOK_SECRET=$(openssl rand -hex 32)

# Required — from OpenAI
OPENAI_API_KEY=sk-proj-...

# Required — Google Drive folder ID
GOOGLE_DRIVE_FOLDER_ID=1AbCdEfGhIjKlMnOpQrStUvWxYz

# Domain (match your Nginx config)
CORS_ORIGINS=https://api.yourdomain.nl
```

### 2.3 Google Service Account Credentials

```bash
# Create credentials directory (already done by bootstrap)
ls -la /var/rinkel/credentials/

# Upload your service account JSON from local machine
# Run this on YOUR LOCAL MACHINE:
scp service-account.json rinkel@YOUR_VPS_IP:/var/rinkel/credentials/

# Back on VPS — secure it
chmod 400 /var/rinkel/credentials/service-account.json
chown rinkel:rinkel /var/rinkel/credentials/service-account.json
```

### 2.4 Update Nginx Domain

Replace the placeholder domain in the Nginx config:

```bash
# Replace yourdomain.nl with your actual domain
sed -i 's/api.yourdomain.nl/api.YOUR-ACTUAL-DOMAIN.nl/g' \
    /opt/rinkel/deploy/nginx/conf.d/rinkel.conf

# Verify
grep "server_name" /opt/rinkel/deploy/nginx/conf.d/rinkel.conf
```

---

## Phase 3 — First Deployment

### 3.1 Make Scripts Executable

```bash
chmod +x /opt/rinkel/deploy/scripts/*.sh
```

### 3.2 Build and Start Services

```bash
cd /opt/rinkel

# Build the image
docker compose -f docker-compose.prod.yml build

# Start all services
docker compose -f docker-compose.prod.yml up -d

# Watch startup logs
docker compose -f docker-compose.prod.yml logs -f --tail=50
```

### 3.3 Verify All Services Healthy

```bash
# Check container status
docker compose -f docker-compose.prod.yml ps

# Expected output:
# NAME           STATUS           PORTS
# rinkel-api     Up (healthy)     8000/tcp
# rinkel-nginx   Up               0.0.0.0:80->80/tcp, 0.0.0.0:443->443/tcp
# rinkel-db      Up (healthy)     5432/tcp   (if using local postgres)

# Health endpoint (through Nginx)
curl -s https://api.yourdomain.nl/health | python3 -m json.tool
```

Expected health response:
```json
{
  "status": "healthy",
  "app": "rinkel-auditor",
  "version": "0.1.0",
  "environment": "production",
  "timestamp": "2026-05-30T08:00:00.000000+00:00"
}
```

### 3.4 Test Webhook Endpoint

```bash
# Test webhook reachability
curl -s -X POST https://api.yourdomain.nl/api/v1/webhooks/rinkel \
  -H "Content-Type: application/json" \
  -d '{"test": true}'

# Should return 401 (no valid signature) — proves the route is reachable
```

---

## Phase 4 — Subsequent Deployments

### 4.1 Standard Deploy

```bash
cd /opt/rinkel

# Pull latest code
git pull origin main

# Deploy (auto-builds, health-checks, and rolls back on failure)
./deploy/scripts/deploy.sh
```

### 4.2 Deploy Specific Version

```bash
./deploy/scripts/deploy.sh --version v1.2.3
```

### 4.3 Emergency Rollback

```bash
./deploy/scripts/deploy.sh --rollback
```

### 4.4 View Live Logs

```bash
# API logs
docker compose -f docker-compose.prod.yml logs -f api

# Nginx access logs (JSON structured)
tail -f /var/log/nginx/rinkel/access.log | python3 -m json.tool

# Cron pipeline logs
tail -f /var/log/rinkel/cron.log
```

---

## Phase 5 — SSL Management

### 5.1 Verify Certificate

```bash
# Check cert expiry
certbot certificates

# Test SSL config
curl -vI https://api.yourdomain.nl 2>&1 | grep -E "SSL|TLS|expire"

# Check via SSL Labs (run from local browser)
# https://www.ssllabs.com/ssltest/analyze.html?d=api.yourdomain.nl
```

### 5.2 Manual Certificate Renewal

```bash
# Test renewal (dry run — no changes)
certbot renew --dry-run

# Force renewal
certbot renew --force-renewal

# Reload Nginx after renewal
docker exec rinkel-nginx nginx -s reload
```

### 5.3 Auto-Renewal

Already configured by the bootstrap script:

```cron
0 3 * * * root certbot renew --quiet --post-hook 'docker exec rinkel-nginx nginx -s reload'
```

---

## Phase 6 — Monitoring

### 6.1 Built-in Health Check

```bash
# Continuous health monitor (run on VPS or external monitor)
watch -n 30 'curl -s https://api.yourdomain.nl/health'
```

### 6.2 Docker Stats

```bash
# Real-time resource usage
docker stats rinkel-api rinkel-nginx rinkel-db

# Typical healthy values:
# api:   CPU <20%, MEM <400MB
# nginx: CPU <5%,  MEM <50MB
# db:    CPU <10%, MEM <200MB
```

### 6.3 Disk Usage Monitoring

```bash
# Check export directory size
du -sh /var/rinkel/exports/

# Check log sizes
du -sh /var/log/rinkel/ /var/log/nginx/rinkel/

# Overall disk
df -h
```

### 6.4 External Uptime Monitoring

Configure a free external monitor with one of:
- **Better Uptime** (betteruptime.com) — 3-min checks, free tier
- **UptimeRobot** — 5-min checks, free tier
- **Freshping** — 1-min checks, free tier

Monitor: `https://api.yourdomain.nl/health` — alert if non-200 response.

### 6.5 Log-Based Alerts

```bash
# Count 5xx errors in last hour
grep '"status":5' /var/log/nginx/rinkel/access.log | \
    awk -F'"time":"' '{print $2}' | cut -c1-16 | \
    awk -v h="$(date -u +%Y-%m-%dT%H)" '$0 ~ h' | wc -l

# Count webhook processing failures in last hour
docker logs rinkel-api 2>&1 | \
    grep "$(date -u +%Y-%m-%dT%H)" | \
    grep -c '"level":"error"' || echo 0
```

---

## Phase 7 — Backup Configuration

### 7.1 Schedule Backups

```bash
# Edit crontab for rinkel user
crontab -u rinkel -e

# Add:
0 2 * * * BACKUP_DIR=/var/backups/rinkel \
           RETENTION_DAYS=14 \
           /opt/rinkel/deploy/scripts/backup.sh \
           >> /var/log/rinkel/backup.log 2>&1
```

### 7.2 Remote Backup with Rclone

```bash
# Install rclone
curl https://rclone.org/install.sh | sudo bash

# Configure S3/B2 remote (interactive)
rclone config

# Test remote
rclone ls your-remote:your-bucket

# Add remote to backup script env
RCLONE_REMOTE="your-remote:your-bucket" \
    /opt/rinkel/deploy/scripts/backup.sh
```

### 7.3 Supabase Backup

```bash
# Supabase provides automated daily backups on Pro plan
# For manual backup from cloud:
pg_dump \
    "postgresql://postgres.[ref]:[pass]@aws-0-eu-central-1.pooler.supabase.com:5432/postgres" \
    --no-owner \
    --format=custom \
    --compress=9 \
    > /var/backups/rinkel/supabase_$(date +%Y%m%d).dump
```

---

## Security Hardening Checklist

| Item | Status | Command to Verify |
|------|--------|-------------------|
| UFW active | ✅ Required | `ufw status` |
| Password SSH disabled | ✅ Required | `grep PasswordAuthentication /etc/ssh/sshd_config` |
| Port 8000 not exposed | ✅ Required | `ufw status \| grep 8000` (should show nothing) |
| App runs as non-root | ✅ Required | `docker exec rinkel-api id` |
| `.env.prod` permissions | ✅ Required | `ls -la /opt/rinkel/.env.prod` (should be 600) |
| Credentials permissions | ✅ Required | `ls -la /var/rinkel/credentials/` (should be 400) |
| Fail2Ban running | ✅ Required | `systemctl status fail2ban` |
| SSL A+ rating | ⭐ Recommended | ssllabs.com |
| HSTS enabled | ⭐ Recommended | After 1 week of stable SSL, uncomment in nginx.conf |
| Docker rootless | 🔮 Advanced | Requires Docker rootless mode setup |
| Secrets management | 🔮 Advanced | HashiCorp Vault or Docker Secrets |

---

## Scaling Recommendations

### Vertical Scaling (single VPS)

```bash
# Increase worker count in .env.prod
WORKERS=4   # (2 × CPU cores) + 1

# Restart to apply
docker compose -f docker-compose.prod.yml up -d api
```

### When to Scale Horizontally

| Signal | Threshold | Action |
|--------|-----------|--------|
| API CPU | >70% sustained | Add workers or new node |
| API Memory | >80% of limit | Increase memory limit |
| Whisper queue | >50 pending | Increase Whisper concurrency |
| Export timeout | >30s | Increase nginx proxy_read_timeout |
| DB connections | >80% of pool | Increase `pool_size` in config |

### Horizontal Scaling (multi-VPS)

When you outgrow a single VPS:

```
                    ┌───────────────────┐
                    │   Load Balancer    │
                    │  (HAProxy / Caddy) │
                    └────────┬──────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
       ┌──────▼─────┐ ┌──────▼─────┐ ┌──────▼─────┐
       │  API Node 1 │ │  API Node 2 │ │  API Node 3 │
       └─────────────┘ └─────────────┘ └─────────────┘
              │              │              │
              └──────────────┼──────────────┘
                             │
                    ┌────────▼────────┐
                    │  Supabase Cloud  │
                    │  (shared DB)     │
                    └─────────────────┘
```

Key requirements for horizontal scaling:
- Move export files to shared storage (S3/GCS/NFS) — not local disk
- Ensure audio temp dirs use shared volume or process on same node as webhook
- Session state is stateless (FastAPI is already stateless)

---

## Failure Recovery Procedures

### API Container Crashed

```bash
# Check what happened
docker compose -f docker-compose.prod.yml logs api --tail=100

# Restart
docker compose -f docker-compose.prod.yml restart api

# If persistent — rollback to last known good version
./deploy/scripts/deploy.sh --rollback
```

### Database Connection Lost

```bash
# Check postgres health
docker compose -f docker-compose.prod.yml ps postgres

# For Supabase outages — check status.supabase.com
# API will retry connections automatically via SQLAlchemy pool

# Manual reconnect test
docker exec rinkel-api python -c "
import asyncio
from app.core.database import get_db_session
from sqlalchemy import text
async def test():
    async with get_db_session() as session:
        result = await session.execute(text('SELECT 1'))
        print('DB OK:', result.scalar())
asyncio.run(test())
"
```

### Nginx Fails

```bash
# Test config before applying
docker exec rinkel-nginx nginx -t

# Reload (zero-downtime)
docker exec rinkel-nginx nginx -s reload

# Full restart (brief downtime)
docker compose -f docker-compose.prod.yml restart nginx
```

### Disk Space Full

```bash
# Emergency cleanup
# 1. Old exports
find /var/rinkel/exports -name "*.jsonl" -mtime +7 -delete

# 2. Docker cleanup
docker system prune -f
docker volume prune -f

# 3. Old logs
journalctl --vacuum-size=500M

# 4. Check what's large
du -sh /* 2>/dev/null | sort -rh | head -20
```

### Whisper API Failures

```bash
# Check OpenAI status
curl -s https://status.openai.com/api/v2/status.json | \
    python3 -c "import sys,json; s=json.load(sys.stdin); print(s['status']['description'])"

# Re-queue failed transcriptions manually
docker exec rinkel-api python -c "
import asyncio
from app.workers.pipeline import retry_failed_transcriptions
asyncio.run(retry_failed_transcriptions())
"
```

---

## Environment Variable Reference

| Variable | Required | Default | Notes |
|----------|:--------:|---------|-------|
| `DATABASE_URL` | ✅ | — | Use Supabase pooler URL |
| `OPENAI_API_KEY` | ✅ | — | Production key with billing |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | ✅ | — | Path inside container |
| `GOOGLE_DRIVE_FOLDER_ID` | ✅ | — | Parent folder for recordings |
| `RINKEL_WEBHOOK_SECRET` | ✅ | — | `openssl rand -hex 32` |
| `WORKERS` | ⭐ | `2` | `(2 × CPU) + 1` |
| `EXPORT_DIR` | ⭐ | `/data/exports` | Must be a mounted volume |
| `AUDIO_TEMP_DIR` | ⭐ | `/data/audio-temp` | Use tmpfs volume |
| `EXPORT_RETENTION_DAYS` | — | `30` | Adjust based on disk size |
| `WHISPER_CHUNK_DURATION_MINUTES` | — | `10` | For >25MB audio files |
| `CORS_ORIGINS` | ✅ | — | Restrict to your domains |

---

## Files Created

```
deploy/
├── nginx/
│   ├── nginx.conf              # Global Nginx config (worker, gzip, SSL, rate zones)
│   └── conf.d/
│       └── rinkel.conf         # Virtual host: SSL, upstream, per-endpoint rate limits
└── scripts/
    ├── vps-bootstrap.sh        # One-time VPS setup (firewall, Docker, user, SSL)
    ├── deploy.sh               # Zero-downtime deploy with auto-rollback
    └── backup.sh               # DB + exports + encrypted env backup

Dockerfile                      # Production multi-stage build (ffmpeg included)
docker-compose.prod.yml         # Production stack (internal networks, tmpfs, bind mounts)
.env.prod.example               # Production env template
```
