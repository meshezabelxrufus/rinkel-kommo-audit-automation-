"""
FastAPI dependency injection providers.

Central place for all shared dependencies (DB sessions, clients, etc.).
"""

from __future__ import annotations

from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import get_db_session


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Provide an async database session as a FastAPI dependency.

    Usage in routes:
        @router.get("/example")
        async def example(db: AsyncSession = Depends(get_db)):
            ...
    """
    async with get_db_session() as session:
        yield session


def get_config() -> Settings:
    """Inject the application settings."""
    return get_settings()
