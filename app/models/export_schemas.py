"""
Pydantic models for the JSONL export engine.

Covers:
- Export job creation requests (filters)
- Export job status responses
- JSONL record structure (audit-ready)
- Claude-compatible output format
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ── Enums ────────────────────────────────────────────────────────────────

class ExportStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TranscriptStatusFilter(str, Enum):
    COMPLETED = "completed"
    PENDING = "pending"
    FAILED = "failed"
    ANY = "any"


# ── Request models ───────────────────────────────────────────────────────

class ExportFilters(BaseModel):
    """Filters for selecting which calls to export."""

    agent_id: str | None = Field(None, description="Filter by agent UUID")
    agent_external_id: str | None = Field(None, description="Filter by Rinkel agent ID")
    date_from: datetime | None = Field(None, description="Start of date range (inclusive)")
    date_to: datetime | None = Field(None, description="End of date range (inclusive)")
    source: str | None = Field(None, description="Filter by call source (e.g., 'rinkel')")
    direction: str | None = Field(None, description="Filter by call direction: inbound/outbound")
    transcript_status: TranscriptStatusFilter = Field(
        TranscriptStatusFilter.COMPLETED,
        description="Filter by transcript status",
    )
    min_duration_seconds: int | None = Field(None, ge=0, description="Minimum call duration")
    max_duration_seconds: int | None = Field(None, ge=0, description="Maximum call duration")
    tags: list[str] | None = Field(None, description="Filter by call tags")


class CreateExportRequest(BaseModel):
    """Request body for creating a new export job."""

    job_name: str | None = Field(None, max_length=255, description="Human-readable job name")
    filters: ExportFilters = Field(default_factory=ExportFilters)


# ── Response models ──────────────────────────────────────────────────────

class ExportJobResponse(BaseModel):
    """Response for a single export job."""

    id: str
    job_name: str | None = None
    status: ExportStatus
    filters: dict[str, Any] = {}
    call_count: int = 0
    file_path: str | None = None
    file_size_bytes: int | None = None
    file_checksum: str | None = None
    date_range_start: datetime | None = None
    date_range_end: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    processing_time_ms: int | None = None
    error_message: str | None = None
    created_at: datetime
    download_url: str | None = None


class ExportJobListResponse(BaseModel):
    """Paginated list of export jobs."""

    jobs: list[ExportJobResponse]
    total: int
    limit: int
    offset: int


class ExportCountResponse(BaseModel):
    """Preview: how many records match the filters."""

    count: int
    filters: dict[str, Any]
    estimated_size_bytes: int | None = None


# ── JSONL record (Claude audit format) ───────────────────────────────────

class AuditCallRecord(BaseModel):
    """
    Single JSONL record for Claude auditing.

    Each line in the export file is one of these objects,
    containing all data needed for Claude to audit the call.
    """

    # ── Identifiers
    call_id: str
    external_call_id: str

    # ── Agent
    agent: AuditAgentInfo | None = None

    # ── Call metadata
    direction: str
    source: str
    caller_number: str | None = None
    caller_name: str | None = None
    callee_number: str | None = None
    callee_name: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_seconds: int = 0
    status: str

    # ── Audio
    recording_url: str | None = None
    audio_drive_url: str | None = None

    # ── Transcript
    transcript: AuditTranscriptInfo | None = None

    # ── Export metadata
    exported_at: str
    export_version: str = "1.0"


class AuditAgentInfo(BaseModel):
    """Agent information in the audit record."""

    agent_id: str
    external_agent_id: str | None = None
    display_name: str | None = None
    email: str | None = None


class AuditTranscriptInfo(BaseModel):
    """Transcript information in the audit record."""

    transcript_id: str
    content: str
    language: str | None = None
    confidence_score: float | None = None
    model_name: str | None = None
    duration_seconds: float | None = None
    segments: list[dict[str, Any]] | None = None
