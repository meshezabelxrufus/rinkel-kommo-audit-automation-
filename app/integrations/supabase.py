"""
Supabase client integration.

Wraps the Supabase Python client for database operations.
"""

from __future__ import annotations

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_client = None


async def get_supabase_client():
    """
    Return a cached Supabase client instance.

    Lazily initialised on first call.
    """
    global _client
    if _client is not None:
        return _client

    settings = get_settings()

    if not settings.supabase_url or not settings.supabase_anon_key:
        logger.warning("supabase_not_configured")
        return None

    # TODO: initialise with supabase-py
    # from supabase import create_client, Client
    # _client = create_client(settings.supabase_url, settings.supabase_anon_key)

    logger.info("supabase_client_initialized")
    return _client
