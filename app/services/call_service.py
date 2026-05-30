"""
Call service — orchestrates the call record lifecycle.

Responsibilities:
- Create call records from webhook payloads
- Update call processing status
- Coordinate with integrations (Drive upload, Whisper)
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.models.schemas import CallStatus, RinkelWebhookPayload

logger = get_logger(__name__)


class CallService:
    """Business logic for call records."""

    async def process_webhook(self, payload: RinkelWebhookPayload) -> dict:
        """
        Handle an incoming Rinkel webhook.

        Pipeline:
        1. Persist call metadata → repository
        2. Download recording → temp storage
        3. Upload to Google Drive → integration
        4. Transcribe with Whisper → integration
        5. Update record with transcript
        """
        logger.info(
            "processing_webhook",
            call_id=payload.call_id,
            direction=payload.direction,
        )
        # TODO: implement pipeline steps
        return {
            "call_id": payload.call_id,
            "status": CallStatus.RECEIVED,
        }

    async def get_call(self, call_id: str) -> dict | None:
        """Retrieve a call record by Rinkel call ID."""
        # TODO: implement via repository
        logger.info("get_call", call_id=call_id)
        return None

    async def list_calls(
        self,
        *,
        status: CallStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """List call records with optional filtering."""
        # TODO: implement via repository
        logger.info("list_calls", status=status, limit=limit, offset=offset)
        return []
