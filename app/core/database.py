"""
Async database engine, session factory, and connection pool.

Uses SQLAlchemy 2.0 async with asyncpg as the driver.
Connection pool is managed at the engine level and configured
for production workloads.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Module-level singletons — initialised lazily
_engine = None
_session_factory = None


def _get_engine():
    """Create or return the cached async engine."""
    global _engine
    if _engine is not None:
        return _engine

    settings = get_settings()
    database_url = settings.database_url

    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is not configured. "
            "Set it in .env (e.g. postgresql+asyncpg://postgres:postgres@localhost:5432/rinkel)"
        )

    # Ensure we're using the async driver
    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    _engine = create_async_engine(
        database_url,
        echo=settings.debug,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,       # verify connections before use
        pool_recycle=300,          # recycle connections after 5 min
        pool_timeout=30,           # wait max 30s for a connection
    )

    logger.info(
        "database_engine_created",
        pool_size=10,
        max_overflow=20,
    )
    return _engine


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Create or return the cached session factory."""
    global _session_factory
    if _session_factory is not None:
        return _session_factory

    _session_factory = async_sessionmaker(
        bind=_get_engine(),
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    return _session_factory


@asynccontextmanager
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Provide a transactional async database session.

    Usage:
        async with get_db_session() as session:
            result = await session.execute(...)
            await session.commit()

    On exception the transaction is rolled back automatically.
    """
    factory = _get_session_factory()
    session = factory()
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def init_db() -> None:
    """Verify database connectivity at startup."""
    engine = _get_engine()
    async with engine.begin() as conn:
        from sqlalchemy import text
        await conn.execute(text("SELECT 1"))
    logger.info("database_connection_verified")


async def close_db() -> None:
    """Dispose of the engine connection pool."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("database_connection_pool_closed")
