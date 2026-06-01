"""
Transcript repository — data access for transcripts table.

Supports:
- Create (from Whisper results)
- Update status and content
- Lookup by call_id
- Listing by status for batch processing
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.repositories.base import BaseRepository


class TranscriptRepository(BaseRepository):
    """CRUD operations for transcripts in PostgreSQL."""

    async def create(
        self,
        *,
        call_id: UUID | str,
        content: str,
        language: str = "nl",
        confidence_score: float | None = None,
        model_name: str = "whisper-1",
        model_version: str | None = None,
        status: str = "completed",
        processing_time_ms: int | None = None,
        segments: list[dict] | None = None,
        cost_usd: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict:
        """
        Insert a new transcript record.

        The transcripts_sync_status trigger will automatically update
        calls.transcript_status to match.

        Args:
            cost_usd: Estimated Whisper API cost in USD (added in migration 00006).
                      Pass None for transcripts produced before cost tracking.

        Returns the created row.
        """
        result = await self.session.execute(
            text("""
                INSERT INTO transcripts (
                    call_id, content, language, confidence_score,
                    model_name, model_version, status, processing_time_ms,
                    segments, cost_usd, metadata
                ) VALUES (
                    CAST(:call_id AS uuid),
                    :content,
                    :language,
                    :confidence_score,
                    :model_name,
                    :model_version,
                    CAST(:status AS transcript_status),
                    :processing_time_ms,
                    CAST(:segments AS jsonb),
                    :cost_usd,
                    CAST(:metadata AS jsonb)
                )
                RETURNING id, call_id, content_length, word_count, language,
                          confidence_score, model_name, status,
                          processing_time_ms, cost_usd, created_at
            """),
            {
                "call_id": str(call_id),
                "content": content,
                "language": language,
                "confidence_score": confidence_score,
                "model_name": model_name,
                "model_version": model_version,
                "status": status,
                "processing_time_ms": processing_time_ms,
                "segments": json.dumps(segments or [], default=str),
                "cost_usd": cost_usd,
                "metadata": json.dumps(metadata or {}, default=str),
            },
        )
        row = result.mappings().first()
        self._logger.info(
            "transcript_created",
            transcript_id=str(row["id"]),
            call_id=str(call_id),
            content_length=row["content_length"],
            word_count=row["word_count"],
            language=language,
            confidence=confidence_score,
            cost_usd=cost_usd,
        )
        return dict(row)

    async def update_status(
        self,
        transcript_id: UUID | str,
        *,
        status: str,
        error_message: str | None = None,
    ) -> dict | None:
        """Update transcript processing status."""
        result = await self.session.execute(
            text("""
                UPDATE transcripts
                SET status = CAST(:status AS transcript_status),
                    error_message = :error_message
                WHERE id = CAST(:id AS uuid)
                RETURNING id, call_id, status, updated_at
            """),
            {
                "id": str(transcript_id),
                "status": status,
                "error_message": error_message,
            },
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def update_content(
        self,
        transcript_id: UUID | str,
        *,
        content: str,
        segments: list[dict] | None = None,
        confidence_score: float | None = None,
        status: str = "completed",
    ) -> dict | None:
        """
        Update transcript content (e.g., after re-transcription or cleanup).
        """
        result = await self.session.execute(
            text("""
                UPDATE transcripts
                SET content = :content,
                    segments = CAST(:segments AS jsonb),
                    confidence_score = :confidence_score,
                    status = CAST(:status AS transcript_status)
                WHERE id = CAST(:id AS uuid)
                RETURNING id, call_id, content_length, status, updated_at
            """),
            {
                "id": str(transcript_id),
                "content": content,
                "segments": json.dumps(segments or [], default=str),
                "confidence_score": confidence_score,
                "status": status,
            },
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def get_by_call_id(self, call_id: UUID | str) -> dict | None:
        """Fetch the latest transcript for a call."""
        result = await self.session.execute(
            text("""
                SELECT id, call_id, content, content_length, word_count, language,
                       confidence_score, model_name, model_version,
                       status, processing_time_ms, error_message,
                       segments, cost_usd, metadata, created_at, updated_at
                FROM transcripts
                WHERE call_id = CAST(:call_id AS uuid)
                ORDER BY created_at DESC
                LIMIT 1
            """),
            {"call_id": str(call_id)},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def get_by_id(self, transcript_id: UUID | str) -> dict | None:
        """Fetch a single transcript by primary key."""
        result = await self.session.execute(
            text("""
                SELECT id, call_id, content, content_length, word_count, language,
                       confidence_score, model_name, model_version,
                       status, processing_time_ms, error_message,
                       segments, cost_usd, metadata, created_at, updated_at
                FROM transcripts
                WHERE id = CAST(:id AS uuid)
            """),
            {"id": str(transcript_id)},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def set_call_transcript_id(
        self,
        call_id: UUID | str,
        transcript_id: UUID | str,
    ) -> None:
        """
        Update calls.transcript_id to link a call to its authoritative transcript.

        Called after a successful transcript create() to wire the new FK
        added in migration 00006.
        """
        await self.session.execute(
            text("""
                UPDATE calls
                SET transcript_id = CAST(:transcript_id AS uuid)
                WHERE id = CAST(:call_id AS uuid)
            """),
            {
                "call_id": str(call_id),
                "transcript_id": str(transcript_id),
            },
        )
        self._logger.info(
            "call_transcript_id_set",
            call_id=str(call_id),
            transcript_id=str(transcript_id),
        )

    async def list_by_status(
        self,
        status: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """List transcripts filtered by status."""
        result = await self.session.execute(
            text("""
                SELECT id, call_id, content_length, language,
                       confidence_score, model_name, status,
                       processing_time_ms, created_at
                FROM transcripts
                WHERE status = CAST(:status AS transcript_status)
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
            """),
            {"status": status, "limit": limit, "offset": offset},
        )
        return [dict(row) for row in result.mappings().all()]

    async def get_stats(self) -> dict:
        """Get transcript statistics for monitoring."""
        result = await self.session.execute(
            text("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE status = 'completed') AS completed,
                    COUNT(*) FILTER (WHERE status = 'failed') AS failed,
                    COUNT(*) FILTER (WHERE status = 'processing') AS processing,
                    AVG(content_length)    FILTER (WHERE status = 'completed') AS avg_length,
                    AVG(word_count)        FILTER (WHERE status = 'completed') AS avg_word_count,
                    AVG(confidence_score)  FILTER (WHERE status = 'completed') AS avg_confidence,
                    AVG(processing_time_ms) FILTER (WHERE status = 'completed') AS avg_processing_ms,
                    SUM(content_length)    FILTER (WHERE status = 'completed') AS total_chars,
                    SUM(cost_usd)          FILTER (WHERE status = 'completed') AS total_cost_usd,
                    AVG(cost_usd)          FILTER (WHERE status = 'completed' AND cost_usd IS NOT NULL) AS avg_cost_usd
                FROM transcripts
            """)
        )
        row = result.mappings().first()
        return dict(row) if row else {}
