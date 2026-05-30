"""
Call processing pipeline worker.

Provides:
- Full pipeline: audio download → Drive upload → transcription
- Single-stage processors (audio only, transcription only)
- Batch processing (polls for pending calls)
- Failed call retry (audio and transcription)
- Stale temp file cleanup
- Background task launcher for FastAPI

Pipeline flow:
  Webhook → process_call_audio() → transcribe_call()
  ┌─ Audio: download → validate → Drive upload → verify
  └─ Transcription: fetch audio → Whisper API → post-process → store

Usage from webhook:
    launch_audio_processing(call_id)

Usage as batch processor:
    await process_pending_calls()      # audio
    await process_pending_transcriptions()  # whisper
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from app.core.database import get_db_session
from app.core.logging import get_logger
from app.repositories.call_repository import CallRepository
from app.services.audio_service import AudioService
from app.services.transcription_service import TranscriptionService

logger = get_logger(__name__)


# ── Full pipeline (audio + transcription) ────────────────────────────────

async def process_call_audio(call_id: str | UUID) -> dict:
    """
    Process audio for a single call, then trigger transcription.

    This is the primary entry point called from the webhook service.
    Runs in a background task (fire-and-forget from the webhook handler).
    """
    call_id_str = str(call_id)
    logger.info("pipeline_start", call_id=call_id_str)

    # ── Phase 1: Audio download + Drive upload ───────────────────────
    try:
        audio_service = AudioService()
        audio_result = await audio_service.process_call_audio(call_id_str)

        logger.info(
            "pipeline_audio_result",
            call_id=call_id_str,
            status=audio_result.get("status"),
            drive_file_id=audio_result.get("drive_file_id"),
        )

        # If audio failed or was skipped, don't proceed to transcription
        if audio_result.get("status") not in ("uploaded", "already_uploaded"):
            return audio_result

    except Exception as e:
        logger.exception(
            "pipeline_audio_error",
            call_id=call_id_str,
            error=str(e),
        )
        return {"status": "error", "phase": "audio", "error": str(e)}

    # ── Phase 2: Whisper transcription ───────────────────────────────
    try:
        transcription_service = TranscriptionService()
        transcript_result = await transcription_service.transcribe_call(call_id_str)

        logger.info(
            "pipeline_transcription_result",
            call_id=call_id_str,
            status=transcript_result.get("status"),
            transcript_id=transcript_result.get("transcript_id"),
            cost_usd=transcript_result.get("cost_usd"),
        )

        return {
            "status": transcript_result.get("status"),
            "call_id": call_id_str,
            "drive_file_id": audio_result.get("drive_file_id"),
            "transcript_id": transcript_result.get("transcript_id"),
            "cost_usd": transcript_result.get("cost_usd"),
        }

    except Exception as e:
        logger.exception(
            "pipeline_transcription_error",
            call_id=call_id_str,
            error=str(e),
        )
        return {
            "status": "error",
            "phase": "transcription",
            "call_id": call_id_str,
            "drive_file_id": audio_result.get("drive_file_id"),
            "error": str(e),
        }


# ── Batch processors ────────────────────────────────────────────────────

async def process_pending_calls(*, batch_size: int = 10) -> list[dict]:
    """
    Poll for calls in 'received' status and process audio.

    Useful as a cron job to catch calls missed by real-time processing.
    """
    logger.info("pipeline_batch_audio_start", batch_size=batch_size)

    async with get_db_session() as session:
        call_repo = CallRepository(session)
        pending = await call_repo.list_pending_processing(limit=batch_size)

    if not pending:
        logger.info("pipeline_batch_audio_no_pending")
        return []

    logger.info("pipeline_batch_audio_found", count=len(pending))

    results = []
    for call in pending:
        audio_service = AudioService()
        result = await audio_service.process_call_audio(str(call["id"]))
        results.append(result)
        await asyncio.sleep(1)

    succeeded = sum(1 for r in results if r.get("status") in ("uploaded", "already_uploaded"))
    failed = sum(1 for r in results if r.get("status") == "error")

    logger.info(
        "pipeline_batch_audio_complete",
        total=len(results),
        succeeded=succeeded,
        failed=failed,
    )
    return results


async def process_pending_transcriptions(*, batch_size: int = 5) -> list[dict]:
    """
    Poll for calls in 'transcribing' status and run Whisper.

    Picks up calls that have audio on Drive but haven't been transcribed.
    """
    logger.info("pipeline_batch_transcription_start", batch_size=batch_size)

    service = TranscriptionService()
    results = await service.transcribe_pending(batch_size=batch_size)

    total_cost = sum(r.get("cost_usd", 0) for r in results)
    logger.info(
        "pipeline_batch_transcription_complete",
        total=len(results),
        total_cost_usd=round(total_cost, 4),
    )
    return results


# ── Retry processors ────────────────────────────────────────────────────

async def retry_failed_audio(*, batch_size: int = 5) -> list[dict]:
    """Retry audio processing for calls with download/upload failures."""
    logger.info("pipeline_retry_audio_start", batch_size=batch_size)

    async with get_db_session() as session:
        call_repo = CallRepository(session)
        failed = await call_repo.list_failed_audio(limit=batch_size)

    if not failed:
        logger.info("pipeline_retry_audio_no_failed")
        return []

    logger.info("pipeline_retry_audio_found", count=len(failed))

    results = []
    for call in failed:
        async with get_db_session() as session:
            call_repo = CallRepository(session)
            await call_repo.update_status(str(call["id"]), status="received")
            await session.commit()

        audio_service = AudioService()
        result = await audio_service.process_call_audio(str(call["id"]))
        results.append(result)
        await asyncio.sleep(2)

    logger.info(
        "pipeline_retry_audio_complete",
        total=len(results),
        succeeded=sum(1 for r in results if r.get("status") in ("uploaded", "already_uploaded")),
    )
    return results


async def retry_failed_transcriptions(*, batch_size: int = 3) -> list[dict]:
    """Retry transcription for calls with transcription failures."""
    logger.info("pipeline_retry_transcription_start", batch_size=batch_size)

    service = TranscriptionService()
    results = await service.retry_failed(batch_size=batch_size)

    logger.info(
        "pipeline_retry_transcription_complete",
        total=len(results),
        succeeded=sum(1 for r in results if r.get("status") == "transcribed"),
    )
    return results


# ── Cleanup ──────────────────────────────────────────────────────────────

async def cleanup_stale_files(*, max_age_hours: int = 24) -> int:
    """Remove stale temporary audio files."""
    service = AudioService()
    cleaned = await service.cleanup_stale_temp_files(max_age_hours=max_age_hours)

    logger.info(
        "pipeline_cleanup_complete",
        files_cleaned=cleaned,
        max_age_hours=max_age_hours,
    )
    return cleaned


# ── Background task launcher ─────────────────────────────────────────────

def launch_audio_processing(call_id: str | UUID) -> asyncio.Task:
    """
    Fire-and-forget: audio + transcription as a background task.

    Called from the webhook service after a call with a recording_url
    is created. The task runs independently — webhook response is not
    delayed by processing.
    """
    task = asyncio.create_task(
        process_call_audio(call_id),
        name=f"pipeline-{call_id}",
    )

    def _on_done(t: asyncio.Task) -> None:
        if t.cancelled():
            logger.warning("pipeline_task_cancelled", call_id=str(call_id))
        elif t.exception():
            logger.error(
                "pipeline_task_failed",
                call_id=str(call_id),
                error=str(t.exception()),
            )
        else:
            result = t.result()
            logger.info(
                "pipeline_task_done",
                call_id=str(call_id),
                status=result.get("status") if result else "unknown",
            )

    task.add_done_callback(_on_done)
    return task
