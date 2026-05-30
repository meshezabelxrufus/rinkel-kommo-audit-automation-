"""
Pydantic schemas for API request/response validation.

Naming convention:
- *Create  → POST body
- *Update  → PATCH body
- *Response → response model
- *Webhook → inbound webhook payload

Rinkel webhook payloads are modeled with extra="allow" to capture
any fields not yet mapped, preserving them in webhook_payload JSONB.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


# ── Enums ────────────────────────────────────────────────────────────────────

class CallDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    INTERNAL = "internal"


class CallStatus(StrEnum):
    RECEIVED = "received"
    DOWNLOADING = "downloading"
    DOWNLOAD_FAILED = "download_failed"
    UPLOADING = "uploading"
    UPLOAD_FAILED = "upload_failed"
    TRANSCRIBING = "transcribing"
    TRANSCRIPTION_FAILED = "transcription_failed"
    TRANSCRIBED = "transcribed"
    EXPORTING = "exporting"
    EXPORTED = "exported"
    AUDITED = "audited"
    ARCHIVED = "archived"


class TranscriptStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"


class WebhookStatus(StrEnum):
    RECEIVED = "received"
    VALIDATED = "validated"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"
    IGNORED = "ignored"


# ── Rinkel Webhook Payload ──────────────────────────────────────────────────

class RinkelCallData(BaseModel):
    """
    Normalized call data extracted from a Rinkel webhook.

    This maps to the Rinkel API call event structure.
    Extra fields are preserved in the raw payload.
    """

    call_id: str = Field(..., description="Unique call identifier from Rinkel")
    direction: str = Field(default="inbound", description="Call direction")
    status: str = Field(default="completed", description="Call status from Rinkel")

    # Caller / callee
    caller_number: str = Field(default="", description="Calling party number (E.164)")
    caller_name: str = Field(default="", description="Caller display name")
    callee_number: str = Field(default="", description="Called party number (E.164)")
    callee_name: str = Field(default="", description="Callee display name")

    # Timing
    started_at: datetime | None = Field(default=None, description="Call start timestamp")
    ended_at: datetime | None = Field(default=None, description="Call end timestamp")
    duration: int = Field(default=0, ge=0, description="Call duration in seconds")
    ring_duration: int = Field(default=0, ge=0, description="Ring time in seconds")

    # Agent
    agent_id: str | None = Field(default=None, description="Rinkel agent identifier")
    agent_name: str | None = Field(default=None, description="Agent display name")
    agent_email: str | None = Field(default=None, description="Agent email address")

    # Recording
    recording_url: str | None = Field(default=None, description="URL to download call recording")
    recording_format: str = Field(default="wav", description="Audio format")

    @field_validator("direction", mode="before")
    @classmethod
    def normalize_direction(cls, v: str) -> str:
        """Normalize direction values from various Rinkel formats."""
        mapping = {
            "in": "inbound",
            "incoming": "inbound",
            "inbound": "inbound",
            "out": "outbound",
            "outgoing": "outbound",
            "outbound": "outbound",
            "internal": "internal",
            "int": "internal",
        }
        return mapping.get(str(v).lower().strip(), "inbound")

    model_config = {"extra": "allow"}


class RinkelWebhookPayload(BaseModel):
    """
    Top-level Rinkel webhook payload.

    Rinkel may wrap call data in different structures depending on
    the event type. This schema handles the common patterns:
    - Direct call data at root level
    - Nested under a 'data' or 'call' key
    - Event-type wrapper with metadata
    """

    event: str = Field(default="call.completed", description="Webhook event type")
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Event timestamp",
    )
    data: RinkelCallData | None = Field(default=None, description="Nested call data")

    # Allow direct call fields at root level (flexible parsing)
    call_id: str | None = Field(default=None, description="Direct call_id if not nested")

    model_config = {"extra": "allow"}

    def extract_call_data(self) -> RinkelCallData:
        """
        Extract normalized call data regardless of payload structure.

        Handles three patterns:
        1. data.call_id exists → use nested data object
        2. root call_id exists → build RinkelCallData from root fields
        3. Neither → raise ValueError
        """
        # Pattern 1: nested data object
        if self.data is not None and self.data.call_id:
            return self.data

        # Pattern 2: flat payload with call_id at root
        if self.call_id:
            extra = self.model_extra or {}
            return RinkelCallData(
                call_id=self.call_id,
                direction=extra.get("direction", "inbound"),
                status=extra.get("status", "completed"),
                caller_number=extra.get("caller_number", ""),
                caller_name=extra.get("caller_name", ""),
                callee_number=extra.get("callee_number", ""),
                callee_name=extra.get("callee_name", ""),
                started_at=extra.get("started_at"),
                ended_at=extra.get("ended_at"),
                duration=extra.get("duration", extra.get("duration_seconds", 0)),
                ring_duration=extra.get("ring_duration", 0),
                agent_id=extra.get("agent_id"),
                agent_name=extra.get("agent_name"),
                agent_email=extra.get("agent_email"),
                recording_url=extra.get("recording_url"),
                recording_format=extra.get("recording_format", "wav"),
            )

        raise ValueError("Webhook payload contains no identifiable call data")

    def build_idempotency_key(self) -> str:
        """Generate a deterministic idempotency key for deduplication."""
        call_data = self.extract_call_data()
        return f"{self.event}:{call_data.call_id}:{self.timestamp.isoformat()}"


# ── API Response Models ─────────────────────────────────────────────────────

class WebhookResponse(BaseModel):
    """Standard webhook acknowledgement response."""

    status: str = "accepted"
    message: str = "Webhook received and queued for processing"
    webhook_event_id: str | None = None
    call_id: str | None = None


class WebhookErrorResponse(BaseModel):
    """Error response for webhook failures."""

    status: str = "error"
    error: str
    detail: Any = None


class CallRecordResponse(BaseModel):
    """API response for a stored call record."""

    id: str
    external_call_id: str
    direction: CallDirection
    status: CallStatus
    caller_number: str
    callee_number: str
    caller_name: str
    callee_name: str
    duration_seconds: int
    recording_url: str | None = None
    audio_drive_file_id: str | None = None
    transcript_status: TranscriptStatus | None = None
    agent_id: str | None = None
    created_at: datetime
    updated_at: datetime


class AgentResponse(BaseModel):
    """API response for an agent record."""

    id: str
    external_agent_id: str | None = None
    display_name: str
    email: str | None = None
    is_active: bool
    created_at: datetime
