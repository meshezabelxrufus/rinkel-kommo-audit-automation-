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

---

## Kommo CRM Audit Layer

A fully self-contained, read-only analytics layer that combines **Kommo CRM leads** with **Rinkel call records** to answer the question:

> **"How is each agent performing across CRM + calls?"**

No database. No API calls. No ingestion layer. Pure computation from local JSON exports.

### Data Flow

```
exports/
  leads.json          ← Kommo API export (448 leads)
  pipelines.json      ← 11 pipelines, 110 stages
  chats.json          ← 321 WhatsApp conversations
  messages_flat.json  ← 154 AI-ready messages
        │
        ▼
  KommoProvider           reads & caches JSON exports (envelope-aware)
        │
        ├──▶ LeadNormalizer        raw leads → NormalizedLead (str|None fields)
        │
        ├──▶ AgentLinkingEngine    Kommo + Rinkel → AgentUnifiedProfile per agent
        │
        ├──▶ MetricsCalculator     conversion rate, engagement score, call coverage
        │
        ├──▶ KommoAuditService     unified high-level service (lazy-cached)
        │
        ├──▶ KommoExportService    JSONL exporter → Claude audit pipeline
        │
        └──▶ AuditEngine           7-step pipeline → AgentAuditReport list
```

### Kommo Layer Services

| Service | File | Purpose |
|---------|------|---------|
| `KommoProvider` | `app/integrations/kommo.py` | Read-only JSON export reader with caching and lookup helpers |
| `LeadNormalizer` | `app/services/lead_normalizer.py` | Converts raw leads → stable `NormalizedLead` (all str\|None) |
| `AgentLinkingEngine` | `app/services/agent_linking_engine.py` | Joins Kommo + Rinkel by `responsible_user_id` |
| `KommoAuditService` | `app/services/kommo_audit_service.py` | Unified high-level API (lazy-cached) |
| `MetricsCalculator` | `app/services/metrics_calculator.py` | Per-agent performance metrics |
| `KommoExportService` | `app/services/kommo_export_service.py` | JSONL exporter with SHA-256 checksum |
| `AuditEngine` | `app/services/audit_engine.py` | Full 7-step pipeline → `AgentAuditReport` |

### AuditEngine — Quick Start

```python
from app.services.audit_engine import AuditEngine

# Kommo-only (no Rinkel calls)
engine = AuditEngine(exports_dir="exports/")
reports = engine.run()

for report in reports:
    print(
        report.agent_id,
        f"leads={report.kommo.total_leads}",
        f"conversion={report.kommo.conversion_rate:.0%}",
        f"score={report.combined.performance_score:.4f}",
    )

# With Rinkel calls + explicit agent ID cross-reference
engine = AuditEngine(
    exports_dir="exports/",
    rinkel_calls=rinkel_call_list,
    agent_id_map={"agent-nl-007": "10359915"},
)
reports = engine.run()                    # sorted by performance_score desc

# Single agent
report = engine.run_for_agent("10359915")

# Summary table
summary = engine.summary()
# {
#   "total_agents": 2,  "total_leads": 448,
#   "avg_performance_score": 0.43,
#   "top_performer": "10359915",
#   ...
# }

# JSONL export
from app.services.kommo_export_service import KommoExportService
exporter = KommoExportService(exports_dir="exports/", output_dir="exports/jsonl/")
result = exporter.export()
# {"file_path": "...", "records_written": 2, "checksum": "...", ...}
```

### AgentAuditReport Structure

```python
AgentAuditReport(
    agent_id = "10359915",

    kommo = KommoSection(
        total_leads      = 299,
        converted_leads  = 1,
        lost_leads       = 77,
        active_leads     = 221,
        conversion_rate  = 0.0033,   # 0.0–1.0
    ),

    rinkel = RinkelSection(
        total_calls       = 0,       # populated when rinkel_calls injected
        avg_call_duration = 0.0,
        inbound_calls     = 0,
        outbound_calls    = 0,
        engagement_score  = 0.1,    # 0.0–1.0
    ),

    combined = CombinedSection(
        performance_score    = 0.1513,  # 0.0–1.0 (primary KPI)
        activity_consistency = 0.5,
        leads_to_calls_ratio = 0.0,
        responsiveness_proxy = 0.0,
        data_source_flags    = {"kommo": True, "rinkel": False},
    ),
)
```

### Performance Score Formula

```
performance_score = 0.40 × conversion_rate       (CRM quality)
                  + 0.35 × engagement_score       (call activity vs leads)
                  + 0.25 × activity_consistency   (present in both systems)
```

### Lead Classification Logic

| Signal | Classification |
|--------|---------------|
| `loss_reason_id IS NOT NULL` | **Lost** (highest priority) |
| `closed_at IS NOT NULL AND loss_reason_id IS NULL` | **Won** |
| Non-editable stage with "ganado/won/opgelost" keyword | **Won** |
| Non-editable stage with "perdido/lost/cancelada" keyword | **Lost** |
| Everything else | **Active** |

### Kommo Layer Test Coverage

```
tests/test_kommo_provider.py         64 tests  ← KommoProvider
tests/test_lead_normalizer.py        68 tests  ← LeadNormalizer
tests/test_agent_linking_engine.py   59 tests  ← AgentLinkingEngine
tests/test_kommo_audit_service.py    81 tests  ← KommoAuditService
tests/test_metrics_calculator.py     82 tests  ← MetricsCalculator
tests/test_kommo_export_service.py   73 tests  ← KommoExportService
tests/test_audit_engine.py           82 tests  ← AuditEngine
────────────────────────────────────────────────────────────────
Total Kommo layer:                  509 tests  — all passing
```

### Running All Tests

```bash
source venv/bin/activate
python -m pytest tests/ -q
# 509 passed in ~1s
```
