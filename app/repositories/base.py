"""
Base repository providing shared database session patterns.

All repositories inherit from BaseRepository to get consistent
session handling and logging.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger


class BaseRepository:
    """Base class for all repositories."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._logger = get_logger(self.__class__.__name__)

    @property
    def session(self) -> AsyncSession:
        return self._session
