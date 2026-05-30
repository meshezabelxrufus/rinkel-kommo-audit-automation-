# Rinkel Call Auditor

Production-grade call auditing pipeline — ingests Rinkel call webhooks, stores metadata in Supabase, uploads audio to Google Drive, transcribes with Whisper, exports JSONL for Claude auditing, and deploys on a hardened VPS with Nginx + Docker.

## Pipeline Overview

```
Rinkel Webhook → FastAPI → Supabase (metadata)
                         → Google Drive (audio archive: year/month/day/agent)
                         → OpenAI Whisper (transcription)
                         → JSONL Export (Claude audit)
```

## Architecture

```
rinkel/
├── app/
│   ├── core/                        # Config, logging, middleware, exceptions, DI
│   │   ├── config.py                # pydantic-settings: all env vars
│   │   ├── logging.py               # structlog (JSON prod / console dev)
│   │   ├── middleware.py            # Request logging + correlation IDs
│   │   ├── exceptions.py            # Custom exception hierarchy + handlers
│   │   └── database.py             # Async SQLAlchemy engine + session factory
│   ├── routers/                     # FastAPI route definitions
│   │   ├── health.py                # GET /health, GET /health/ready
│   │   ├── webhooks.py              # POST /api/v1/webhooks/rinkel
│   │   └── exports.py               # POST /api/v1/exports + streaming + download
│   ├── models/                      # Pydantic schemas
│   │   ├── schemas.py               # Webhook + call schemas
│   │   └── export_schemas.py        # Export filters, job responses, AuditCallRecord
│   ├── services/                    # Business logic layer
│   │   ├── call_service.py          # Call record lifecycle orchestration
│   │   ├── transcription_service.py # Whisper orchestration + Dutch post-processing
│   │   ├── export_service.py        # JSONL generation, streaming, job lifecycle
│   │   └── audit_prompts.py         # Claude audit prompt builders (6 workflows)
│   ├── repositories/                # Data access layer (SQLAlchemy text queries)
│   │   ├── call_repository.py       # Call CRUD + status updates
│   │   ├── agent_repository.py      # Agent upsert
│   │   ├── transcript_repository.py # Transcript persist + stats
│   │   └── export_repository.py     # Export job CRUD + filtered batch queries
│   ├── integrations/                # External API clients
│   │   ├── google_drive.py          # Drive upload (service account, year/mo/day/agent)
│   │   ├── whisper.py               # Whisper API: chunking, backoff, cost tracking
│   │   └── supabase.py              # Supabase client wrapper
│   ├── workers/                     # Background task processing
│   │   └── pipeline.py              # call audio → transcription chain
│   └── main.py                      # FastAPI app factory + lifespan
│
├── deploy/                          # Production deployment assets
│   ├── nginx/
│   │   ├── nginx.conf               # Global: rate zones, gzip, upstream, TLS
│   │   └── conf.d/rinkel.conf       # Virtual host: SSL, per-endpoint rate limits
│   └── scripts/
│       ├── vps-bootstrap.sh         # One-time VPS setup (Ubuntu 22.04/24.04)
│       ├── deploy.sh                # Zero-downtime deploy + auto-rollback
│       └── backup.sh                # DB + exports + encrypted env backup
│
├── docs/
│   ├── deployment-guide.md          # Full 7-phase VPS deployment guide
│   └── claude-audit-workflows.md    # Claude prompt templates + audit patterns
│
├── supabase/migrations/             # SQL schema migrations
│
├── Dockerfile                       # Multi-stage (deps → runtime), ffmpeg, non-root
├── docker-compose.yml               # Local dev: API + PostgreSQL
├── docker-compose.prod.yml          # Production: resource limits, tmpfs, networks
├── .env.example                     # Dev environment template
├── .env.prod.example                # Production environment template
└── requirements.txt                 # Pinned Python dependencies
```

## Quick Start — Local Development

### 1. Prerequisites

- Python 3.12+
- Docker & Docker Compose
- PostgreSQL (via Docker) or Supabase project

### 2. Without Docker

```bash
python3.12 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your actual values
uvicorn app.main:app --reload --port 8000
```

### 3. With Docker (dev)

```bash
cp .env.example .env
docker compose up --build
```

### 4. Verify

```bash
curl http://localhost:8000/health
curl http://localhost:8000/docs    # Swagger (DEBUG=true only)
```

## Production Deployment

See the full guide: [`docs/deployment-guide.md`](docs/deployment-guide.md)

### Quickstart (Ubuntu 22.04 VPS)

```bash
# 1. Bootstrap VPS (one-time — installs Docker, UFW, Certbot, Fail2Ban)
DOMAIN=api.yourdomain.nl EMAIL=you@company.nl \
  sudo bash deploy/scripts/vps-bootstrap.sh

# 2. Configure production environment
cp .env.prod.example .env.prod
nano .env.prod          # fill in all required values
chmod 600 .env.prod

# 3. Upload Google service account credentials
scp service-account.json rinkel@VPS_IP:/var/rinkel/credentials/

# 4. First deploy
./deploy/scripts/deploy.sh

# 5. Verify via HTTPS
curl https://api.yourdomain.nl/health
```

### Subsequent Deploys

```bash
git pull && ./deploy/scripts/deploy.sh          # standard
./deploy/scripts/deploy.sh --rollback           # emergency rollback
./deploy/scripts/deploy.sh --version v1.2.3     # specific version
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Liveness check |
| `GET` | `/health/ready` | Readiness check (DB included) |
| `POST` | `/api/v1/webhooks/rinkel` | Rinkel call event ingestion |
| `POST` | `/api/v1/exports` | Create JSONL export job |
| `POST` | `/api/v1/exports/stream` | Stream JSONL directly |
| `POST` | `/api/v1/exports/preview` | Count records before export |
| `GET` | `/api/v1/exports` | List export jobs |
| `GET` | `/api/v1/exports/{id}` | Export job status |
| `GET` | `/api/v1/exports/{id}/download` | Download JSONL file |
| `DELETE` | `/api/v1/exports/{id}` | Cancel export job |

## Claude Audit Workflows

See [`docs/claude-audit-workflows.md`](docs/claude-audit-workflows.md) for prompt templates.

| Workflow | Purpose | Model |
|----------|---------|-------|
| Full Audit | 5-dimension QA scorecard | Sonnet |
| Escalation Detection | Anger, repeat, manager request | Haiku |
| Sentiment Analysis | Temporal trajectory + CSAT | Sonnet |
| Compliance Audit | Protocol checklist (7 rules) | Sonnet |
| Batch Agent Scoring | Multi-call aggregate + coaching | Sonnet |
| Topic Classification | Issue category + churn risk | Haiku |

## Configuration

All settings via environment variables. See [`.env.example`](.env.example) for dev and [`.env.prod.example`](.env.prod.example) for production.

| Variable | Required | Description |
|----------|:--------:|-------------|
| `DATABASE_URL` | ✅ | PostgreSQL async connection string |
| `OPENAI_API_KEY` | ✅ | OpenAI key for Whisper transcription |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | ✅ | Path to GCP service account JSON |
| `GOOGLE_DRIVE_FOLDER_ID` | ✅ | Root Drive folder for recordings |
| `RINKEL_WEBHOOK_SECRET` | ✅ | HMAC secret for webhook validation |
| `SUPABASE_URL` | ✅ | Supabase project URL |
| `SUPABASE_ANON_KEY` | ✅ | Supabase anonymous key |
| `WORKERS` | — | uvicorn worker count (default: `2`) |
| `EXPORT_DIR` | — | JSONL export directory (default: `./exports`) |
| `EXPORT_RETENTION_DAYS` | — | Export file retention (default: `30`) |

## Conventions

- **Async-first**: All I/O operations use `async`/`await`
- **Structured logging**: JSON logs in production via `structlog`
- **Correlation IDs**: Every request carries `X-Request-ID`
- **Layered architecture**: Router → Service → Repository → Integration
- **Non-root Docker**: Container runs as `appuser` (UID 1000)
- **No secrets in image**: All credentials via env vars or volume-mounted files
- **Batched DB reads**: Export engine fetches in configurable batch sizes (default 100)
- **tmpfs for audio**: Ephemeral audio temp files stored in RAM-backed tmpfs
