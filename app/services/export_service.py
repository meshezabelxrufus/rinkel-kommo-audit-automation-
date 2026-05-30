"""
JSONL export service — generates audit-ready exports for Claude.

Handles:
1. Export job lifecycle (create → running → completed/failed)
2. Memory-efficient streaming via async generators
3. Batched DB queries with cursor pagination
4. JSONL file writing with SHA-256 checksum
5. Call marking (exported_at + last_export_id)
6. Count previews before export
7. Stale export cleanup

The JSONL format outputs one JSON object per line, where each
line is a complete call record with agent + transcript data.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator
from uuid import UUID

from app.core.config import get_settings
from app.core.database import get_db_session
from app.core.logging import get_logger
from app.models.export_schemas import (
    AuditAgentInfo,
    AuditCallRecord,
    AuditTranscriptInfo,
    ExportFilters,
)
from app.repositories.export_repository import ExportRepository

logger = get_logger(__name__)


class ExportService:
    """
    Orchestrates JSONL export generation.

    Usage:
        service = ExportService()

        # Preview
        count = await service.preview(filters)

        # Generate file
        result = await service.create_export(job_name="daily", filters=filters)

        # Stream (for HTTP response)
        async for line in service.stream_jsonl(filters):
            yield line
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._export_dir = Path(self._settings.export_dir)

    # ── Preview ──────────────────────────────────────────────────────────

    async def preview(self, filters: ExportFilters) -> dict:
        """
        Count matching records without generating an export.

        Returns count and estimated file size.
        """
        filter_dict = filters.model_dump(exclude_none=True, mode="json")

        async with get_db_session() as session:
            repo = ExportRepository(session)
            count = await repo.count_exportable_calls(filter_dict)

        # Rough estimate: ~800 bytes per JSONL line
        estimated_bytes = count * 800

        return {
            "count": count,
            "filters": filter_dict,
            "estimated_size_bytes": estimated_bytes,
        }

    # ── File-based export ────────────────────────────────────────────────

    async def create_export(
        self,
        *,
        job_name: str | None = None,
        filters: ExportFilters | None = None,
    ) -> dict:
        """
        Create a full JSONL export file on disk.

        Steps:
        1. Create export job record (pending)
        2. Count matching calls
        3. Stream-write JSONL to file
        4. Compute checksum
        5. Mark calls as exported
        6. Update job as completed

        Returns the completed job metadata.
        """
        filters = filters or ExportFilters()
        filter_dict = filters.model_dump(exclude_none=True, mode="json")
        start_time = time.perf_counter()

        # ── Step 1: Create job ───────────────────────────────────────────
        async with get_db_session() as session:
            repo = ExportRepository(session)
            job = await repo.create_job(
                job_name=job_name,
                filter_criteria=filter_dict,
                date_range_start=filters.date_from,
                date_range_end=filters.date_to,
            )
            await session.commit()

        job_id = str(job["id"])
        logger.info("export_job_created", job_id=job_id, filters=filter_dict)

        try:
            # ── Step 2: Mark as running ──────────────────────────────────
            async with get_db_session() as session:
                repo = ExportRepository(session)
                await repo.update_job_status(job_id, status="running")
                await session.commit()

            # ── Step 3: Count records ────────────────────────────────────
            async with get_db_session() as session:
                repo = ExportRepository(session)
                total_count = await repo.count_exportable_calls(filter_dict)

            if total_count == 0:
                async with get_db_session() as session:
                    repo = ExportRepository(session)
                    elapsed = int((time.perf_counter() - start_time) * 1000)
                    await repo.update_job_status(
                        job_id,
                        status="completed",
                        call_count=0,
                        processing_time_ms=elapsed,
                    )
                    await session.commit()

                logger.info("export_job_empty", job_id=job_id)
                return {"job_id": job_id, "status": "completed", "call_count": 0}

            # Safety cap
            max_records = self._settings.export_max_records
            if total_count > max_records:
                logger.warning(
                    "export_capped",
                    total=total_count,
                    cap=max_records,
                )
                total_count = max_records

            # ── Step 4: Write JSONL file ─────────────────────────────────
            self._export_dir.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            safe_name = (job_name or "export").replace(" ", "_")[:50]
            filename = f"{safe_name}_{timestamp}_{job_id[:8]}.jsonl"
            file_path = self._export_dir / filename

            written_count = 0
            exported_call_ids: list[str] = []
            sha256 = hashlib.sha256()

            with open(file_path, "w", encoding="utf-8") as f:
                async for record in self._generate_records(
                    filter_dict, max_records=max_records
                ):
                    line = record.model_dump_json() + "\n"
                    f.write(line)
                    sha256.update(line.encode("utf-8"))
                    exported_call_ids.append(record.call_id)
                    written_count += 1

            file_size = file_path.stat().st_size
            checksum = sha256.hexdigest()

            logger.info(
                "export_file_written",
                job_id=job_id,
                path=str(file_path),
                records=written_count,
                size_bytes=file_size,
            )

            # ── Step 5: Mark calls as exported ───────────────────────────
            batch_size = 500
            for i in range(0, len(exported_call_ids), batch_size):
                batch = exported_call_ids[i : i + batch_size]
                async with get_db_session() as session:
                    repo = ExportRepository(session)
                    await repo.mark_calls_exported(batch, job_id)
                    await session.commit()

            # ── Step 6: Complete job ─────────────────────────────────────
            elapsed = int((time.perf_counter() - start_time) * 1000)

            async with get_db_session() as session:
                repo = ExportRepository(session)
                await repo.update_job_status(
                    job_id,
                    status="completed",
                    call_count=written_count,
                    file_path=str(file_path),
                    file_size_bytes=file_size,
                    file_checksum=checksum,
                    processing_time_ms=elapsed,
                    metadata={
                        "filename": filename,
                        "records_matched": total_count,
                        "records_exported": written_count,
                    },
                )
                await session.commit()

            logger.info(
                "export_job_completed",
                job_id=job_id,
                call_count=written_count,
                file_size_bytes=file_size,
                processing_time_ms=elapsed,
            )

            return {
                "job_id": job_id,
                "status": "completed",
                "call_count": written_count,
                "file_path": str(file_path),
                "file_size_bytes": file_size,
                "file_checksum": checksum,
                "processing_time_ms": elapsed,
            }

        except Exception as e:
            logger.exception("export_job_failed", job_id=job_id, error=str(e))

            async with get_db_session() as session:
                repo = ExportRepository(session)
                elapsed = int((time.perf_counter() - start_time) * 1000)
                await repo.update_job_status(
                    job_id,
                    status="failed",
                    error_message=str(e)[:500],
                    processing_time_ms=elapsed,
                )
                await session.commit()

            return {
                "job_id": job_id,
                "status": "failed",
                "error": str(e),
            }

    # ── Streaming export (for HTTP responses) ────────────────────────────

    async def stream_jsonl(
        self,
        filters: ExportFilters | None = None,
    ) -> AsyncGenerator[bytes, None]:
        """
        Stream JSONL records as bytes for an HTTP response.

        Memory-efficient: yields one line at a time, never holds
        the full export in memory.
        """
        filters = filters or ExportFilters()
        filter_dict = filters.model_dump(exclude_none=True, mode="json")
        max_records = self._settings.export_max_records

        count = 0
        async for record in self._generate_records(filter_dict, max_records=max_records):
            line = record.model_dump_json() + "\n"
            yield line.encode("utf-8")
            count += 1

        logger.info("export_stream_complete", records=count)

    # ── Record generator ─────────────────────────────────────────────────

    async def _generate_records(
        self,
        filter_dict: dict[str, Any],
        *,
        max_records: int = 50000,
    ) -> AsyncGenerator[AuditCallRecord, None]:
        """
        Async generator that yields AuditCallRecord objects.

        Fetches from DB in batches to keep memory bounded.
        Each batch is a separate DB session/connection.
        """
        batch_size = self._settings.export_batch_size
        offset = 0
        total_yielded = 0
        now_iso = datetime.now(timezone.utc).isoformat()

        while total_yielded < max_records:
            async with get_db_session() as session:
                repo = ExportRepository(session)
                batch = await repo.fetch_export_batch(
                    filter_dict,
                    limit=batch_size,
                    offset=offset,
                )

            if not batch:
                break

            for row in batch:
                if total_yielded >= max_records:
                    break

                record = self._row_to_audit_record(row, exported_at=now_iso)
                yield record
                total_yielded += 1

            offset += batch_size

            # Log progress every 1000 records
            if total_yielded % 1000 == 0:
                logger.info(
                    "export_progress",
                    records=total_yielded,
                    offset=offset,
                )

    # ── Row → AuditCallRecord conversion ─────────────────────────────────

    def _row_to_audit_record(
        self,
        row: dict[str, Any],
        *,
        exported_at: str,
    ) -> AuditCallRecord:
        """Convert a joined DB row into a Claude-ready audit record."""
        # Agent info
        agent = None
        if row.get("agent_id"):
            agent = AuditAgentInfo(
                agent_id=str(row["agent_id"]),
                external_agent_id=row.get("agent_external_id"),
                display_name=row.get("agent_name"),
                email=row.get("agent_email"),
            )

        # Transcript info
        transcript = None
        if row.get("transcript_id") and row.get("transcript_content"):
            # Parse segments from JSONB
            segments = row.get("transcript_segments")
            if isinstance(segments, str):
                try:
                    segments = json.loads(segments)
                except (json.JSONDecodeError, TypeError):
                    segments = None

            transcript = AuditTranscriptInfo(
                transcript_id=str(row["transcript_id"]),
                content=row["transcript_content"],
                language=row.get("transcript_language"),
                confidence_score=float(row["transcript_confidence"])
                if row.get("transcript_confidence")
                else None,
                model_name=row.get("transcript_model"),
                segments=segments,
            )

        return AuditCallRecord(
            call_id=str(row["call_id"]),
            external_call_id=row["external_call_id"],
            agent=agent,
            direction=row.get("direction", "inbound"),
            source=row.get("source", "rinkel"),
            caller_number=row.get("caller_number"),
            caller_name=row.get("caller_name"),
            callee_number=row.get("callee_number"),
            callee_name=row.get("callee_name"),
            started_at=row["started_at"].isoformat() if row.get("started_at") else None,
            ended_at=row["ended_at"].isoformat() if row.get("ended_at") else None,
            duration_seconds=row.get("duration_seconds", 0),
            status=row.get("call_status", "unknown"),
            recording_url=row.get("recording_url"),
            audio_drive_url=row.get("audio_drive_url"),
            transcript=transcript,
            exported_at=exported_at,
        )

    # ── Job management ───────────────────────────────────────────────────

    async def get_job(self, job_id: str) -> dict | None:
        """Fetch export job details."""
        async with get_db_session() as session:
            repo = ExportRepository(session)
            return await repo.get_job(job_id)

    async def list_jobs(
        self,
        *,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """List export jobs."""
        async with get_db_session() as session:
            repo = ExportRepository(session)
            return await repo.list_jobs(status=status, limit=limit, offset=offset)

    async def cancel_job(self, job_id: str) -> dict | None:
        """Cancel a pending or running export job."""
        async with get_db_session() as session:
            repo = ExportRepository(session)
            job = await repo.get_job(job_id)
            if not job:
                return None
            if job["status"] not in ("pending", "running"):
                return job

            result = await repo.update_job_status(job_id, status="cancelled")
            await session.commit()
            logger.info("export_job_cancelled", job_id=job_id)
            return result

    # ── Cleanup ──────────────────────────────────────────────────────────

    async def cleanup_old_exports(self, *, max_age_days: int | None = None) -> int:
        """
        Remove export files older than max_age_days.

        Deletes the files from disk and updates the job records.
        """
        max_age = max_age_days or self._settings.export_retention_days
        cutoff = datetime.now(timezone.utc).timestamp() - (max_age * 86400)
        cleaned = 0

        if not self._export_dir.exists():
            return 0

        for f in self._export_dir.glob("*.jsonl"):
            if f.stat().st_mtime < cutoff:
                try:
                    f.unlink()
                    cleaned += 1
                    logger.info("export_file_cleaned", path=str(f))
                except Exception as e:
                    logger.warning(
                        "export_file_cleanup_failed",
                        path=str(f),
                        error=str(e),
                    )

        if cleaned:
            logger.info("export_cleanup_complete", files_cleaned=cleaned)

        return cleaned
