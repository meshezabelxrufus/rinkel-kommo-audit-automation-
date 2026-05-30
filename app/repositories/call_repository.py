"""
Call repository — data access for calls table.

Supports:
- Create (from webhook data)
- Upsert by external_call_id (idempotent re-processing)
- Status updates
- Queries by status, agent, time range
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.repositories.base import BaseRepository


class CallRepository(BaseRepository):
    """CRUD operations for call records in PostgreSQL."""

    async def upsert(
        self,
        *,
        external_call_id: str,
        agent_id: UUID | str | None = None,
        source: str = "rinkel",
        direction: str = "inbound",
        caller_number: str = "",
        caller_name: str = "",
        callee_number: str = "",
        callee_name: str = "",
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        duration_seconds: int = 0,
        ring_duration_seconds: int = 0,
        status: str = "received",
        recording_url: str | None = None,
        audio_format: str = "wav",
        webhook_payload: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict:
        """
        Insert or update a call record by external_call_id.

        On conflict (duplicate external_call_id), updates mutable fields.
        This makes webhook re-delivery safe — same call won't be duplicated.

        Returns the upserted row.
        """
        result = await self.session.execute(
            text("""
                INSERT INTO calls (
                    external_call_id, agent_id, source, direction,
                    caller_number, caller_name, callee_number, callee_name,
                    started_at, ended_at, duration_seconds, ring_duration_seconds,
                    status, recording_url, audio_format,
                    webhook_payload, metadata
                ) VALUES (
                    :external_call_id,
                    CAST(:agent_id AS uuid),
                    :source,
                    CAST(:direction AS call_direction),
                    :caller_number, :caller_name, :callee_number, :callee_name,
                    :started_at, :ended_at, :duration_seconds, :ring_duration_seconds,
                    CAST(:status AS call_status),
                    :recording_url, :audio_format,
                    CAST(:webhook_payload AS jsonb),
                    CAST(:metadata AS jsonb)
                )
                ON CONFLICT (external_call_id) DO UPDATE SET
                    agent_id = COALESCE(EXCLUDED.agent_id, calls.agent_id),
                    caller_number = COALESCE(NULLIF(EXCLUDED.caller_number, ''), calls.caller_number),
                    caller_name = COALESCE(NULLIF(EXCLUDED.caller_name, ''), calls.caller_name),
                    callee_number = COALESCE(NULLIF(EXCLUDED.callee_number, ''), calls.callee_number),
                    callee_name = COALESCE(NULLIF(EXCLUDED.callee_name, ''), calls.callee_name),
                    started_at = COALESCE(EXCLUDED.started_at, calls.started_at),
                    ended_at = COALESCE(EXCLUDED.ended_at, calls.ended_at),
                    duration_seconds = GREATEST(EXCLUDED.duration_seconds, calls.duration_seconds),
                    ring_duration_seconds = GREATEST(EXCLUDED.ring_duration_seconds, calls.ring_duration_seconds),
                    recording_url = COALESCE(EXCLUDED.recording_url, calls.recording_url),
                    webhook_payload = EXCLUDED.webhook_payload,
                    updated_at = NOW()
                RETURNING id, external_call_id, agent_id, status, direction,
                          caller_number, callee_number, duration_seconds,
                          recording_url, transcript_status, created_at, updated_at
            """),
            {
                "external_call_id": external_call_id,
                "agent_id": str(agent_id) if agent_id else None,
                "source": source,
                "direction": direction,
                "caller_number": caller_number,
                "caller_name": caller_name,
                "callee_number": callee_number,
                "callee_name": callee_name,
                "started_at": started_at,
                "ended_at": ended_at,
                "duration_seconds": duration_seconds,
                "ring_duration_seconds": ring_duration_seconds,
                "status": status,
                "recording_url": recording_url,
                "audio_format": audio_format,
                "webhook_payload": json.dumps(webhook_payload or {}, default=str),
                "metadata": json.dumps(metadata or {}, default=str),
            },
        )
        row = result.mappings().first()
        self._logger.info(
            "call_upserted",
            call_id=str(row["id"]),
            external_call_id=external_call_id,
            status=status,
        )
        return dict(row)

    async def get_by_external_id(self, external_call_id: str) -> dict | None:
        """Fetch a call record by its Rinkel external call ID."""
        result = await self.session.execute(
            text("""
                SELECT id, external_call_id, agent_id, source, direction,
                       caller_number, caller_name, callee_number, callee_name,
                       started_at, ended_at, duration_seconds, status,
                       recording_url, audio_drive_file_id, audio_drive_url,
                       transcript_status, error_message, retry_count,
                       created_at, updated_at
                FROM calls
                WHERE external_call_id = :external_call_id
            """),
            {"external_call_id": external_call_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def get_by_id(self, call_id: UUID | str) -> dict | None:
        """Fetch a single call record by primary key."""
        result = await self.session.execute(
            text("""
                SELECT id, external_call_id, agent_id, source, direction,
                       caller_number, caller_name, callee_number, callee_name,
                       started_at, ended_at, duration_seconds, status,
                       recording_url, audio_drive_file_id, audio_drive_url,
                       transcript_status, error_message, retry_count,
                       created_at, updated_at
                FROM calls
                WHERE id = CAST(:id AS uuid)
            """),
            {"id": str(call_id)},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def update_status(
        self,
        call_id: UUID | str,
        *,
        status: str,
        error_message: str | None = None,
    ) -> dict | None:
        """
        Update the processing status of a call.

        The calls_audit_status_change trigger will automatically
        log this transition to audit_logs.
        """
        result = await self.session.execute(
            text("""
                UPDATE calls
                SET status = CAST(:status AS call_status),
                    error_message = :error_message,
                    retry_count = CASE
                        WHEN :status LIKE '%%_failed' THEN retry_count + 1
                        ELSE retry_count
                    END,
                    last_retry_at = CASE
                        WHEN :status LIKE '%%_failed' THEN NOW()
                        ELSE last_retry_at
                    END
                WHERE id = CAST(:id AS uuid)
                RETURNING id, external_call_id, status, retry_count, updated_at
            """),
            {
                "id": str(call_id),
                "status": status,
                "error_message": error_message,
            },
        )
        row = result.mappings().first()
        if row:
            self._logger.info(
                "call_status_updated",
                call_id=str(call_id),
                new_status=status,
            )
        return dict(row) if row else None

    async def list_by_status(
        self,
        status: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """List calls filtered by processing status."""
        result = await self.session.execute(
            text("""
                SELECT id, external_call_id, agent_id, direction, status,
                       caller_number, callee_number, duration_seconds,
                       recording_url, transcript_status,
                       created_at, updated_at
                FROM calls
                WHERE status = CAST(:status AS call_status)
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
            """),
            {"status": status, "limit": limit, "offset": offset},
        )
        return [dict(row) for row in result.mappings().all()]

    async def list_pending_processing(self, *, limit: int = 20) -> list[dict]:
        """
        Get calls that need audio processing.

        Returns calls in 'received' status that have a recording_url.
        Uses the idx_calls_status_pending partial index.
        """
        result = await self.session.execute(
            text("""
                SELECT id, external_call_id, recording_url, agent_id
                FROM calls
                WHERE status = 'received'
                  AND recording_url IS NOT NULL
                ORDER BY created_at ASC
                LIMIT :limit
            """),
            {"limit": limit},
        )
        return [dict(row) for row in result.mappings().all()]

    async def update_audio_metadata(
        self,
        call_id: UUID | str,
        *,
        audio_drive_file_id: str,
        audio_drive_url: str,
        audio_size_bytes: int | None = None,
        status: str = "transcribing",
    ) -> dict | None:
        """
        Update call record with Google Drive upload metadata.

        Called after successful audio upload to Drive.
        """
        result = await self.session.execute(
            text("""
                UPDATE calls
                SET audio_drive_file_id = :audio_drive_file_id,
                    audio_drive_url = :audio_drive_url,
                    audio_size_bytes = :audio_size_bytes,
                    status = CAST(:status AS call_status)
                WHERE id = CAST(:id AS uuid)
                RETURNING id, external_call_id, audio_drive_file_id, audio_drive_url,
                          audio_size_bytes, status, updated_at
            """),
            {
                "id": str(call_id),
                "audio_drive_file_id": audio_drive_file_id,
                "audio_drive_url": audio_drive_url,
                "audio_size_bytes": audio_size_bytes,
                "status": status,
            },
        )
        row = result.mappings().first()
        if row:
            self._logger.info(
                "call_audio_metadata_updated",
                call_id=str(call_id),
                drive_file_id=audio_drive_file_id,
            )
        return dict(row) if row else None

    async def list_failed_audio(self, *, limit: int = 20) -> list[dict]:
        """
        Get calls with failed audio processing for retry.

        Returns calls with download_failed or upload_failed status
        that haven't exceeded max retries.
        Uses the idx_calls_failed partial index.
        """
        result = await self.session.execute(
            text("""
                SELECT id, external_call_id, recording_url, agent_id,
                       status, retry_count, error_message, last_retry_at
                FROM calls
                WHERE status IN ('download_failed', 'upload_failed')
                  AND retry_count < :max_retries
                ORDER BY last_retry_at ASC NULLS FIRST
                LIMIT :limit
            """),
            {"max_retries": 3, "limit": limit},
        )
        return [dict(row) for row in result.mappings().all()]
