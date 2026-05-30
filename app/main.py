"""
FastAPI application factory.

Assembles the app with:
- CORS middleware
- Request logging middleware
- Exception handlers
- Router includes
- Lifespan events (startup/shutdown)
- Database connection pool lifecycle
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import get_logger, setup_logging
from app.core.middleware import RequestLoggingMiddleware
from app.routers import health, webhooks, exports


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan — runs on startup and shutdown."""
    logger = get_logger("lifespan")
    settings = get_settings()

    logger.info(
        "startup",
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
    )

    # ── Startup: initialise database connection pool ─────────────────────
    if settings.database_url:
        try:
            from app.core.database import init_db
            await init_db()
            logger.info("database_connected")
        except Exception as e:
            logger.error("database_connection_failed", error=str(e))
            # Don't crash — allow health endpoints to report degraded status
    else:
        logger.warning("database_url_not_configured")

    # ── Startup: ensure export directory exists ──────────────────────────
    from pathlib import Path
    export_dir = Path(settings.export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)

    yield

    # ── Shutdown: close database connection pool ─────────────────────────
    logger.info("shutdown")
    try:
        from app.core.database import close_db
        await close_db()
    except Exception as e:
        logger.error("database_close_failed", error=str(e))


def create_app() -> FastAPI:
    """Application factory — returns a fully configured FastAPI instance."""
    settings = get_settings()

    # Logging must be configured before anything else
    setup_logging()

    app = FastAPI(
        title="Rinkel Call Auditor",
        description=(
            "Call auditing pipeline: ingests Rinkel webhooks, stores metadata, "
            "uploads audio to Google Drive, transcribes with Whisper, "
            "and exports JSONL for Claude auditing."
        ),
        version=settings.app_version,
        docs_url="/docs" if settings.debug else None,
        redoc_url="/redoc" if settings.debug else None,
        openapi_url="/openapi.json" if settings.debug else None,
        lifespan=lifespan,
    )

    # ── Middleware (order matters — outermost first) ──────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestLoggingMiddleware)

    # ── Exception handlers ───────────────────────────────────────────────
    register_exception_handlers(app)

    # ── Routers ──────────────────────────────────────────────────────────
    app.include_router(health.router)
    app.include_router(webhooks.router, prefix="/api/v1")
    app.include_router(exports.router, prefix="/api/v1")

    return app


# Module-level app instance for uvicorn: `uvicorn app.main:app`
app = create_app()
