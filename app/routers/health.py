"""
Health-check & readiness endpoints.

GET /health       → liveness probe
GET /health/ready → readiness probe (checks DB connectivity)
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

from app.core.config import get_settings
from app.core.logging import get_logger

router = APIRouter(tags=["health"])
logger = get_logger(__name__)


@router.get("/health", summary="Liveness probe")
async def health() -> dict:
    """Return basic health status. Used by Docker / K8s liveness probes."""
    settings = get_settings()
    return {
        "status": "healthy",
        "app": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/health/ready", summary="Readiness probe")
async def readiness() -> dict:
    """
    Check downstream dependencies.

    Currently a placeholder — will verify Supabase / DB connectivity
    once the database layer is wired.
    """
    checks: dict[str, str] = {}

    # TODO: add actual checks
    # try:
    #     async with get_async_session() as session:
    #         await session.execute(text("SELECT 1"))
    #     checks["database"] = "ok"
    # except Exception:
    #     checks["database"] = "unavailable"

    checks["database"] = "not_configured"

    all_ok = all(v == "ok" or v == "not_configured" for v in checks.values())

    return {
        "status": "ready" if all_ok else "degraded",
        "checks": checks,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
