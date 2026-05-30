"""
Export repository — data access for export_jobs table and export queries.

Supports:
- Export job CRUD (create, update status, list)
- Filtered call queries for export generation
- Cursor-based pagination for memory-efficient streaming
- Count queries for export previews
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, AsyncGenerator
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.repositories.base import BaseRepository

logger = get_logger(__name__)


class ExportRepository(BaseRepository):
    """Data access for export jobs and export data queries."""

    # ── Export job CRUD ───────────────────────────────────────────────────

    async def create_job(
        self,
        *,
        job_name: str | None = None,
        filter_criteria: dict[str, Any] | None = None,
        date_range_start: datetime | None = None,
        date_range_end: datetime | None = None,
    ) -> dict:
        """Create a new export job record."""
        result = await self.session.execute(
            text("""
                INSERT INTO export_jobs (
                    job_name, filter_criteria, date_range_start, date_range_end,
                    status
                ) VALUES (
                    :job_name,
                    CAST(:filter_criteria AS jsonb),
                    :date_range_start,
                    :date_range_end,
                    'pending'
                )
                RETURNING id, job_name, status, filter_criteria,
                          date_range_start, date_range_end, created_at
            """),
            {
                "job_name": job_name,
                "filter_criteria": json.dumps(filter_criteria or {}),
                "date_range_start": date_range_start,
                "date_range_end": date_range_end,
            },
        )
        row = result.mappings().first()
        self._logger.info(
            "export_job_created",
            job_id=str(row["id"]),
            job_name=job_name,
        )
        return dict(row)

    async def update_job_status(
        self,
        job_id: UUID | str,
        *,
        status: str,
        call_count: int | None = None,
        file_path: str | None = None,
        file_size_bytes: int | None = None,
        file_checksum: str | None = None,
        processing_time_ms: int | None = None,
        error_message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict | None:
        """Update export job status and metadata."""
        result = await self.session.execute(
            text("""
                UPDATE export_jobs
                SET status = CAST(:status AS export_status),
                    call_count = COALESCE(:call_count, call_count),
                    file_path = COALESCE(:file_path, file_path),
                    file_size_bytes = COALESCE(:file_size_bytes, file_size_bytes),
                    file_checksum = COALESCE(:file_checksum, file_checksum),
                    processing_time_ms = COALESCE(:processing_time_ms, processing_time_ms),
                    error_message = :error_message,
                    metadata = COALESCE(CAST(:metadata AS jsonb), metadata),
                    started_at = CASE
                        WHEN :status = 'running' AND started_at IS NULL THEN NOW()
                        ELSE started_at
                    END,
                    completed_at = CASE
                        WHEN :status IN ('completed', 'failed', 'cancelled') THEN NOW()
                        ELSE completed_at
                    END
                WHERE id = CAST(:id AS uuid)
                RETURNING id, status, call_count, file_path, file_size_bytes,
                          started_at, completed_at, processing_time_ms
            """),
            {
                "id": str(job_id),
                "status": status,
                "call_count": call_count,
                "file_path": file_path,
                "file_size_bytes": file_size_bytes,
                "file_checksum": file_checksum,
                "processing_time_ms": processing_time_ms,
                "error_message": error_message,
                "metadata": json.dumps(metadata) if metadata else None,
            },
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def get_job(self, job_id: UUID | str) -> dict | None:
        """Fetch a single export job by ID."""
        result = await self.session.execute(
            text("""
                SELECT id, job_name, status, filter_criteria, call_count,
                       date_range_start, date_range_end,
                       file_path, file_size_bytes, file_checksum,
                       started_at, completed_at, processing_time_ms,
                       error_message, metadata, created_at, updated_at
                FROM export_jobs
                WHERE id = CAST(:id AS uuid)
            """),
            {"id": str(job_id)},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def list_jobs(
        self,
        *,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """List export jobs with optional status filter. Returns (jobs, total)."""
        # Count query
        count_sql = "SELECT COUNT(*) AS total FROM export_jobs"
        count_params: dict[str, Any] = {}

        if status:
            count_sql += " WHERE status = CAST(:status AS export_status)"
            count_params["status"] = status

        count_result = await self.session.execute(text(count_sql), count_params)
        total = count_result.scalar() or 0

        # Data query
        data_sql = """
            SELECT id, job_name, status, filter_criteria, call_count,
                   date_range_start, date_range_end,
                   file_path, file_size_bytes, file_checksum,
                   started_at, completed_at, processing_time_ms,
                   error_message, created_at
            FROM export_jobs
        """
        data_params: dict[str, Any] = {"limit": limit, "offset": offset}

        if status:
            data_sql += " WHERE status = CAST(:status AS export_status)"
            data_params["status"] = status

        data_sql += " ORDER BY created_at DESC LIMIT :limit OFFSET :offset"

        result = await self.session.execute(text(data_sql), data_params)
        jobs = [dict(row) for row in result.mappings().all()]

        return jobs, total

    # ── Export data queries ───────────────────────────────────────────────

    async def count_exportable_calls(
        self,
        filters: dict[str, Any],
    ) -> int:
        """Count calls matching the export filters (for preview)."""
        where_clauses, params = self._build_where_clauses(filters)
        where_sql = " AND ".join(where_clauses) if where_clauses else "TRUE"

        result = await self.session.execute(
            text(f"""
                SELECT COUNT(*) AS total
                FROM calls c
                LEFT JOIN agents a ON a.id = c.agent_id
                LEFT JOIN LATERAL (
                    SELECT status
                    FROM transcripts
                    WHERE call_id = c.id
                    ORDER BY created_at DESC
                    LIMIT 1
                ) t ON TRUE
                WHERE {where_sql}
            """),
            params,
        )
        return result.scalar() or 0

    async def fetch_export_batch(
        self,
        filters: dict[str, Any],
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """
        Fetch a batch of calls with agent and transcript data joined.

        Uses cursor-based pagination (offset) for streaming.
        Returns fully joined rows ready for JSONL serialization.
        """
        where_clauses, params = self._build_where_clauses(filters)
        where_sql = " AND ".join(where_clauses) if where_clauses else "TRUE"
        params["limit"] = limit
        params["offset"] = offset

        result = await self.session.execute(
            text(f"""
                SELECT
                    c.id AS call_id,
                    c.external_call_id,
                    c.direction::text AS direction,
                    c.source,
                    c.caller_number,
                    c.caller_name,
                    c.callee_number,
                    c.callee_name,
                    c.started_at,
                    c.ended_at,
                    c.duration_seconds,
                    c.status::text AS call_status,
                    c.recording_url,
                    c.audio_drive_url,
                    c.created_at AS call_created_at,

                    a.id AS agent_id,
                    a.external_agent_id,
                    a.display_name AS agent_name,
                    a.email AS agent_email,

                    t.id AS transcript_id,
                    t.content AS transcript_content,
                    t.language AS transcript_language,
                    t.confidence_score AS transcript_confidence,
                    t.model_name AS transcript_model,
                    t.segments AS transcript_segments,
                    t.processing_time_ms AS transcript_processing_ms

                FROM calls c
                LEFT JOIN agents a ON a.id = c.agent_id
                LEFT JOIN LATERAL (
                    SELECT *
                    FROM transcripts
                    WHERE call_id = c.id
                    ORDER BY created_at DESC
                    LIMIT 1
                ) t ON TRUE
                WHERE {where_sql}
                ORDER BY c.created_at ASC
                LIMIT :limit OFFSET :offset
            """),
            params,
        )
        return [dict(row) for row in result.mappings().all()]

    async def mark_calls_exported(
        self,
        call_ids: list[str],
        export_job_id: str,
    ) -> int:
        """Mark calls as exported by setting last_export_id and exported_at."""
        if not call_ids:
            return 0

        # Build parameterised IN clause
        placeholders = ", ".join(f"CAST(:id_{i} AS uuid)" for i in range(len(call_ids)))
        params: dict[str, Any] = {
            "export_job_id": export_job_id,
        }
        for i, cid in enumerate(call_ids):
            params[f"id_{i}"] = str(cid)

        result = await self.session.execute(
            text(f"""
                UPDATE calls
                SET last_export_id = CAST(:export_job_id AS uuid),
                    exported_at = NOW()
                WHERE id IN ({placeholders})
                RETURNING id
            """),
            params,
        )
        count = len(result.fetchall())
        return count

    # ── Filter builder ───────────────────────────────────────────────────

    def _build_where_clauses(
        self,
        filters: dict[str, Any],
    ) -> tuple[list[str], dict[str, Any]]:
        """
        Build SQL WHERE clauses from export filters.

        Returns (list_of_clause_strings, params_dict).
        """
        clauses: list[str] = []
        params: dict[str, Any] = {}

        # Agent filter
        if filters.get("agent_id"):
            clauses.append("c.agent_id = CAST(:f_agent_id AS uuid)")
            params["f_agent_id"] = filters["agent_id"]
        elif filters.get("agent_external_id"):
            clauses.append("a.external_agent_id = :f_agent_ext_id")
            params["f_agent_ext_id"] = filters["agent_external_id"]

        # Date range
        if filters.get("date_from"):
            clauses.append("c.created_at >= :f_date_from")
            params["f_date_from"] = filters["date_from"]
        if filters.get("date_to"):
            clauses.append("c.created_at <= :f_date_to")
            params["f_date_to"] = filters["date_to"]

        # Source
        if filters.get("source"):
            clauses.append("c.source = :f_source")
            params["f_source"] = filters["source"]

        # Direction
        if filters.get("direction"):
            clauses.append("c.direction = CAST(:f_direction AS call_direction)")
            params["f_direction"] = filters["direction"]

        # Transcript status
        ts = filters.get("transcript_status", "completed")
        if ts and ts != "any":
            clauses.append("t.status = CAST(:f_transcript_status AS transcript_status)")
            params["f_transcript_status"] = ts

        # Duration range
        if filters.get("min_duration_seconds") is not None:
            clauses.append("c.duration_seconds >= :f_min_dur")
            params["f_min_dur"] = filters["min_duration_seconds"]
        if filters.get("max_duration_seconds") is not None:
            clauses.append("c.duration_seconds <= :f_max_dur")
            params["f_max_dur"] = filters["max_duration_seconds"]

        # Tags
        if filters.get("tags"):
            clauses.append("c.tags @> CAST(:f_tags AS text[])")
            params["f_tags"] = "{" + ",".join(filters["tags"]) + "}"

        return clauses, params
