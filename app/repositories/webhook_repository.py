"""
Webhook event repository — data access for webhook_events table.

This table is append-only (immutable after processing).
Supports idempotency checks and status transitions.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.repositories.base import BaseRepository


class WebhookEventRepository(BaseRepository):
    """CRUD operations for webhook_events in PostgreSQL."""

    async def check_idempotency(self, idempotency_key: str) -> dict | None:
        """
        Check if a webhook with this idempotency key already exists.

        Returns the existing row or None if not found.
        Used to prevent duplicate processing of webhook deliveries.
        """
        result = await self.session.execute(
            text("""
                SELECT id, status, call_id, created_at
                FROM webhook_events
                WHERE idempotency_key = :key
                LIMIT 1
            """),
            {"key": idempotency_key},
        )
        row = result.mappings().first()
        if row:
            self._logger.info(
                "webhook_duplicate_detected",
                idempotency_key=idempotency_key,
                existing_id=str(row["id"]),
            )
            return dict(row)
        return None

    async def create(
        self,
        *,
        source: str,
        event_type: str,
        idempotency_key: str | None,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        signature: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> dict:
        """
        Insert a new webhook event record.

        Returns the created row as a dict.
        """
        result = await self.session.execute(
            text("""
                INSERT INTO webhook_events (
                    source, event_type, idempotency_key, payload,
                    headers, signature, status,
                    ip_address, user_agent
                ) VALUES (
                    :source, :event_type, :idempotency_key,
                    CAST(:payload AS jsonb),
                    CAST(:headers AS jsonb),
                    :signature, 'received',
                    CAST(:ip_address AS inet), :user_agent
                )
                RETURNING id, source, event_type, idempotency_key, status, created_at
            """),
            {
                "source": source,
                "event_type": event_type,
                "idempotency_key": idempotency_key,
                "payload": json.dumps(payload, default=str),
                "headers": json.dumps(headers or {}, default=str),
                "signature": signature,
                "ip_address": ip_address,
                "user_agent": user_agent,
            },
        )
        row = result.mappings().first()
        self._logger.info(
            "webhook_event_created",
            webhook_event_id=str(row["id"]),
            event_type=event_type,
        )
        return dict(row)

    async def update_status(
        self,
        webhook_event_id: UUID | str,
        *,
        status: str,
        call_id: UUID | str | None = None,
        error_message: str | None = None,
        processing_time_ms: int | None = None,
    ) -> dict | None:
        """Update the processing status of a webhook event."""
        result = await self.session.execute(
            text("""
                UPDATE webhook_events
                SET status = CAST(:status AS webhook_status),
                    call_id = CAST(:call_id AS uuid),
                    error_message = :error_message,
                    processing_time_ms = :processing_time_ms,
                    processed_at = CASE WHEN :status IN ('processed', 'failed', 'ignored')
                                        THEN NOW() ELSE processed_at END
                WHERE id = CAST(:id AS uuid)
                RETURNING id, status, call_id, processed_at
            """),
            {
                "id": str(webhook_event_id),
                "status": status,
                "call_id": str(call_id) if call_id else None,
                "error_message": error_message,
                "processing_time_ms": processing_time_ms,
            },
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def get_by_id(self, webhook_event_id: UUID | str) -> dict | None:
        """Fetch a single webhook event by ID."""
        result = await self.session.execute(
            text("SELECT * FROM webhook_events WHERE id = CAST(:id AS uuid)"),
            {"id": str(webhook_event_id)},
        )
        row = result.mappings().first()
        return dict(row) if row else None
