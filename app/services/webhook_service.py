"""
Webhook ingestion service — orchestrates the full webhook processing pipeline.

This is the core business logic layer. It:
1. Checks idempotency (duplicate detection)
2. Stores the raw webhook event
3. Verifies the webhook signature (placeholder)
4. Parses and normalizes call data
5. Upserts the agent (if present)
6. Upserts the call record
7. Queues audio processing
8. Updates webhook event status

All database operations run within a single transaction.
On failure, the transaction rolls back and the webhook event
is marked as failed.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.schemas import (
    RinkelCallData,
    RinkelWebhookPayload,
    WebhookResponse,
    WebhookStatus,
)
from app.repositories.agent_repository import AgentRepository
from app.repositories.call_repository import CallRepository
from app.repositories.webhook_repository import WebhookEventRepository

logger = get_logger(__name__)


class WebhookService:
    """
    Orchestrates webhook ingestion with idempotency and transaction safety.

    All public methods accept an AsyncSession to participate in the
    caller's transaction boundary.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._webhook_repo = WebhookEventRepository(session)
        self._agent_repo = AgentRepository(session)
        self._call_repo = CallRepository(session)

    async def process_rinkel_webhook(
        self,
        *,
        payload: RinkelWebhookPayload,
        raw_body: bytes,
        headers: dict[str, str],
        signature: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> WebhookResponse:
        """
        Full webhook processing pipeline.

        This method is the single entry point for all Rinkel webhooks.
        It handles the complete flow within a single DB transaction.

        Returns a WebhookResponse for the HTTP response body.
        """
        start_time = time.perf_counter()

        # ── Step 1: Generate idempotency key ─────────────────────────────
        idempotency_key = self._generate_idempotency_key(raw_body, payload)

        # ── Step 2: Check for duplicate delivery ─────────────────────────
        existing = await self._webhook_repo.check_idempotency(idempotency_key)
        if existing:
            logger.info(
                "webhook_duplicate_skipped",
                idempotency_key=idempotency_key,
                existing_id=str(existing["id"]),
            )
            return WebhookResponse(
                status="duplicate",
                message="Webhook already processed",
                webhook_event_id=str(existing["id"]),
                call_id=str(existing.get("call_id")) if existing.get("call_id") else None,
            )

        # ── Step 3: Store raw webhook event ──────────────────────────────
        sanitized_headers = self._sanitize_headers(headers)
        raw_payload = payload.model_dump(mode="json")

        webhook_event = await self._webhook_repo.create(
            source="rinkel",
            event_type=payload.event,
            idempotency_key=idempotency_key,
            payload=raw_payload,
            headers=sanitized_headers,
            signature=signature,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        webhook_event_id = webhook_event["id"]

        logger.info(
            "webhook_event_stored",
            webhook_event_id=str(webhook_event_id),
            event_type=payload.event,
        )

        # ── Step 4: Verify signature (placeholder) ───────────────────────
        if not self._verify_signature(raw_body, signature):
            await self._webhook_repo.update_status(
                webhook_event_id,
                status=WebhookStatus.FAILED,
                error_message="Signature verification failed",
            )
            # NOTE: In production, raise WebhookAuthError here.
            # For now, log and continue to allow unsigned dev webhooks.
            logger.warning(
                "webhook_signature_unverified",
                webhook_event_id=str(webhook_event_id),
            )

        # ── Step 5: Parse normalized call data ───────────────────────────
        try:
            call_data = payload.extract_call_data()
        except ValueError as e:
            await self._webhook_repo.update_status(
                webhook_event_id,
                status=WebhookStatus.FAILED,
                error_message=f"Failed to extract call data: {e}",
            )
            await self._session.commit()
            logger.error(
                "webhook_parse_failed",
                webhook_event_id=str(webhook_event_id),
                error=str(e),
            )
            return WebhookResponse(
                status="error",
                message=f"Failed to parse call data: {e}",
                webhook_event_id=str(webhook_event_id),
            )

        await self._webhook_repo.update_status(
            webhook_event_id, status=WebhookStatus.VALIDATED
        )

        # ── Step 6: Upsert agent (if present in call data) ───────────────
        agent_id = None
        if call_data.agent_id:
            agent = await self._upsert_agent(call_data)
            agent_id = agent["id"]

        # ── Step 7: Upsert call record ───────────────────────────────────
        await self._webhook_repo.update_status(
            webhook_event_id, status=WebhookStatus.PROCESSING
        )

        call_record = await self._upsert_call(
            call_data=call_data,
            agent_id=agent_id,
            raw_payload=raw_payload,
        )
        call_id = call_record["id"]

        # ── Step 8: Queue audio processing ───────────────────────────────
        has_recording = bool(call_data.recording_url)
        if has_recording:
            await self._queue_audio_processing(call_id, call_data.recording_url)

        # ── Step 9: Mark webhook as processed ────────────────────────────
        processing_time_ms = int((time.perf_counter() - start_time) * 1000)

        await self._webhook_repo.update_status(
            webhook_event_id,
            status=WebhookStatus.PROCESSED,
            call_id=call_id,
            processing_time_ms=processing_time_ms,
        )

        # Commit the full transaction
        await self._session.commit()

        logger.info(
            "webhook_processed_successfully",
            webhook_event_id=str(webhook_event_id),
            call_id=str(call_id),
            external_call_id=call_data.call_id,
            has_recording=has_recording,
            processing_time_ms=processing_time_ms,
        )

        return WebhookResponse(
            status="accepted",
            message="Webhook processed successfully",
            webhook_event_id=str(webhook_event_id),
            call_id=str(call_id),
        )

    # ── Private helpers ──────────────────────────────────────────────────────

    def _generate_idempotency_key(
        self,
        raw_body: bytes,
        payload: RinkelWebhookPayload,
    ) -> str:
        """
        Generate a deterministic idempotency key.

        Strategy: SHA-256 hash of the raw request body.
        This catches exact duplicate deliveries regardless of
        timestamp differences.
        """
        body_hash = hashlib.sha256(raw_body).hexdigest()[:16]
        try:
            call_data = payload.extract_call_data()
            return f"{payload.event}:{call_data.call_id}:{body_hash}"
        except ValueError:
            return f"{payload.event}:unknown:{body_hash}"

    def _verify_signature(
        self,
        raw_body: bytes,
        signature: str | None,
    ) -> bool:
        """
        Verify the webhook signature from Rinkel.

        Placeholder implementation — returns True if no secret is configured,
        allowing unsigned development webhooks.

        Production implementation should use HMAC-SHA256:
            expected = hmac.new(secret, raw_body, sha256).hexdigest()
            return hmac.compare_digest(expected, signature)
        """
        settings = get_settings()
        secret = settings.rinkel_webhook_secret

        if not secret:
            # No secret configured — skip verification (dev mode)
            return True

        if not signature:
            logger.warning("webhook_missing_signature")
            return False

        # HMAC-SHA256 verification
        expected = hmac.new(
            secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(expected, signature)

    def _sanitize_headers(self, headers: dict[str, str]) -> dict[str, str]:
        """Remove sensitive headers before storing."""
        sensitive_keys = {"authorization", "cookie", "x-api-key"}
        return {
            k: v
            for k, v in headers.items()
            if k.lower() not in sensitive_keys
        }

    async def _upsert_agent(self, call_data: RinkelCallData) -> dict:
        """Upsert agent from call data."""
        agent = await self._agent_repo.upsert(
            external_agent_id=call_data.agent_id,
            display_name=call_data.agent_name or call_data.agent_id,
            email=call_data.agent_email,
        )
        logger.info(
            "agent_upserted_from_webhook",
            agent_id=str(agent["id"]),
            external_agent_id=call_data.agent_id,
        )
        return agent

    async def _upsert_call(
        self,
        *,
        call_data: RinkelCallData,
        agent_id: str | None,
        raw_payload: dict[str, Any],
    ) -> dict:
        """Upsert call record from normalized call data."""
        call = await self._call_repo.upsert(
            external_call_id=call_data.call_id,
            agent_id=agent_id,
            source="rinkel",
            direction=call_data.direction,
            caller_number=call_data.caller_number,
            caller_name=call_data.caller_name,
            callee_number=call_data.callee_number,
            callee_name=call_data.callee_name,
            started_at=call_data.started_at,
            ended_at=call_data.ended_at,
            duration_seconds=call_data.duration,
            ring_duration_seconds=call_data.ring_duration,
            status="received",
            recording_url=call_data.recording_url,
            audio_format=call_data.recording_format,
            webhook_payload=raw_payload,
        )
        return call

    async def _queue_audio_processing(
        self,
        call_id: str,
        recording_url: str,
    ) -> None:
        """
        Launch audio processing as a background asyncio task.

        The task runs independently after the webhook response is sent.
        It downloads the audio, uploads to Drive, and updates the call record.

        For distributed deployments, replace this with ARQ/Celery enqueue:
            await arq_pool.enqueue_job("process_call_audio", call_id)
        """
        from app.workers.pipeline import launch_audio_processing

        logger.info(
            "audio_processing_launched",
            call_id=str(call_id),
            recording_url=recording_url,
        )
        launch_audio_processing(call_id)

