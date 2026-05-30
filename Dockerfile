# ══════════════════════════════════════════════════════════════════════════════
# Rinkel Call Auditor — Production Dockerfile
#
# Multi-stage build:
#   Stage 1 (deps)    - Compiles Python wheels in a build environment
#   Stage 2 (runtime) - Minimal runtime image, no build tools
#
# Security:
#   - Non-root appuser (UID 1000)
#   - No build tools in runtime layer
#   - Read-only filesystem compatible (data dirs via volume mounts)
#   - No secrets baked in (all via env vars at runtime)
#
# Build:
#   docker build -t rinkel-auditor:latest .
#
# Build with specific tag:
#   docker build -t rinkel-auditor:$(git rev-parse --short HEAD) .
# ══════════════════════════════════════════════════════════════════════════════

# ── Stage 1: Dependency builder ───────────────────────────────────────────────
FROM python:3.12-slim AS deps

WORKDIR /build

# Disable Python output buffering and .pyc files in all stages
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install only what's needed to compile asyncpg + google-auth
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
        && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Compile wheels into /install — isolates from system Python
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2: Production runtime ───────────────────────────────────────────────
FROM python:3.12-slim AS runtime

LABEL maintainer="devops@company.nl" \
      org.opencontainers.image.title="rinkel-auditor" \
      org.opencontainers.image.description="Rinkel call auditing pipeline" \
      org.opencontainers.image.version="0.1.0"

# Python env flags — applied for all processes
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    # uvicorn workers (overridden by CMD)
    WORKERS=2 \
    PORT=8000

# Security: create non-root user before anything else
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid 1000 --no-create-home --shell /sbin/nologin appuser

WORKDIR /app

# Runtime system dependencies only — no gcc, no dev headers
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
        ffmpeg \
        && \
    rm -rf /var/lib/apt/lists/* && \
    # Verify ffmpeg available (required for audio chunking)
    ffmpeg -version > /dev/null 2>&1

# Copy compiled packages from builder
COPY --from=deps /install /usr/local

# Copy application source
COPY --chown=appuser:appuser app/ ./app/

# Create persistent data directories — mounted as volumes in production
RUN mkdir -p \
        /data/exports \
        /data/audio-temp \
        /data/credentials \
        /data/logs \
    && chown -R appuser:appuser /data

# Switch to non-root before defining CMD
USER appuser

EXPOSE 8000

# Liveness probe: FastAPI health endpoint
HEALTHCHECK \
    --interval=30s \
    --timeout=10s \
    --start-period=20s \
    --retries=3 \
    CMD curl -f http://localhost:${PORT:-8000}/health || exit 1

# Production: use gunicorn with uvicorn workers for multi-worker support
# - Workers = (2 × CPU cores) + 1 is the standard formula
# - Adjust WORKERS via environment variable
CMD ["sh", "-c", \
    "python -m uvicorn app.main:app \
    --host 0.0.0.0 \
    --port ${PORT:-8000} \
    --workers ${WORKERS:-2} \
    --log-level ${LOG_LEVEL:-info} \
    --access-log \
    --no-use-colors \
    --timeout-keep-alive 30"]
