"""
Transcription service — orchestrates the Whisper transcription pipeline.

Handles:
1. Fetch call + verify audio exists on Drive
2. Download audio from Drive (temp file)
3. Transcribe with Whisper API
4. Post-process transcript (clean text)
5. Persist transcript + segments to DB
6. Update call status
7. Clean up temp file
8. Cost tracking and logging

All operations use the AudioService's temp dir and cleanup patterns.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import UUID

from app.core.config import get_settings
from app.core.database import get_db_session
from app.core.logging import get_logger
from app.integrations.whisper import TranscriptionResult, WhisperClient
from app.repositories.call_repository import CallRepository
from app.repositories.transcript_repository import TranscriptRepository

logger = get_logger(__name__)


class TranscriptionError(Exception):
    """Base error for transcription failures."""

    def __init__(self, message: str, call_id: str, retryable: bool = True) -> None:
        self.call_id = call_id
        self.retryable = retryable
        super().__init__(message)


class TranscriptionService:
    """
    Orchestrates the full transcription lifecycle for a call.

    Usage:
        service = TranscriptionService()
        result = await service.transcribe_call(call_id)
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._whisper: WhisperClient | None = None

    @property
    def whisper(self) -> WhisperClient:
        if self._whisper is None:
            self._whisper = WhisperClient()
        return self._whisper

    # ── Main entry point ─────────────────────────────────────────────────

    async def transcribe_call(self, call_id: str | UUID) -> dict:
        """
        Full transcription pipeline for a single call.

        Steps:
        1. Fetch call record, verify it's ready for transcription
        2. Download audio from Drive to temp file
        3. Transcribe with Whisper API
        4. Post-process the transcript text
        5. Store transcript + segments in DB
        6. Update call status to 'transcribed'
        7. Clean up temp file

        Returns a dict with transcription result metadata.
        """
        call_id_str = str(call_id)
        start_time = time.perf_counter()

        logger.info("transcription_pipeline_start", call_id=call_id_str)

        # ── Step 1: Fetch and validate call record ───────────────────────
        async with get_db_session() as session:
            call_repo = CallRepository(session)
            call = await call_repo.get_by_id(call_id_str)

        if not call:
            logger.error("transcription_call_not_found", call_id=call_id_str)
            return {"status": "error", "error": "Call not found"}

        # Check if already transcribed
        if call.get("status") == "transcribed":
            async with get_db_session() as session:
                transcript_repo = TranscriptRepository(session)
                existing = await transcript_repo.get_by_call_id(call_id_str)
            if existing:
                logger.info(
                    "transcription_already_complete",
                    call_id=call_id_str,
                    transcript_id=str(existing["id"]),
                )
                return {
                    "status": "already_transcribed",
                    "transcript_id": str(existing["id"]),
                }

        # Verify audio is available
        recording_url = call.get("recording_url")
        drive_file_id = call.get("audio_drive_file_id")

        if not recording_url and not drive_file_id:
            logger.warning("transcription_no_audio", call_id=call_id_str)
            return {"status": "skipped", "reason": "No audio available"}

        # ── Step 2: Update status → transcribing ─────────────────────────
        async with get_db_session() as session:
            call_repo = CallRepository(session)
            await call_repo.update_status(call_id_str, status="transcribing")
            await session.commit()

        temp_path: Path | None = None
        try:
            # ── Step 3: Get audio file ───────────────────────────────────
            # If we have a local temp file from the audio pipeline, use it.
            # Otherwise download from Drive or original URL.
            temp_path = await self._get_audio_file(
                call_id=call_id_str,
                recording_url=recording_url,
                drive_file_id=drive_file_id,
            )

            # ── Step 4: Transcribe ───────────────────────────────────────
            # Build a domain-specific prompt for Dutch call center context
            prompt = self._build_prompt(call)

            whisper_result = await self.whisper.transcribe(
                temp_path,
                language=self._settings.whisper_language,
                prompt=prompt,
            )

            # ── Step 5: Post-process ─────────────────────────────────────
            cleaned_text = self._post_process_transcript(whisper_result.text)
            segments_data = self._serialize_segments(whisper_result)

            # ── Step 6: Persist to DB ────────────────────────────────────
            async with get_db_session() as session:
                transcript_repo = TranscriptRepository(session)

                # Build metadata
                transcript_metadata = {
                    "cost_usd": whisper_result.cost_usd,
                    "chunk_count": whisper_result.chunk_count,
                    "audio_duration_seconds": whisper_result.duration_seconds,
                    "warnings": whisper_result.warnings,
                    "raw_text_length": len(whisper_result.text),
                    "cleaned_text_length": len(cleaned_text),
                }

                transcript = await transcript_repo.create(
                    call_id=call_id_str,
                    content=cleaned_text,
                    language=whisper_result.language,
                    confidence_score=whisper_result.confidence_score,
                    model_name=whisper_result.model,
                    model_version=whisper_result.model_version,
                    status="completed",
                    processing_time_ms=whisper_result.processing_time_ms,
                    segments=segments_data,
                    metadata=transcript_metadata,
                )
                await session.commit()

            transcript_id = str(transcript["id"])

            # ── Step 7: Update call status ───────────────────────────────
            async with get_db_session() as session:
                call_repo = CallRepository(session)
                await call_repo.update_status(call_id_str, status="transcribed")
                await session.commit()

            total_time_ms = int((time.perf_counter() - start_time) * 1000)

            logger.info(
                "transcription_pipeline_complete",
                call_id=call_id_str,
                transcript_id=transcript_id,
                language=whisper_result.language,
                text_length=len(cleaned_text),
                segments=len(whisper_result.segments),
                confidence=round(whisper_result.confidence_score, 4) if whisper_result.confidence_score else None,
                cost_usd=round(whisper_result.cost_usd, 4),
                whisper_time_ms=whisper_result.processing_time_ms,
                total_time_ms=total_time_ms,
            )

            return {
                "status": "transcribed",
                "call_id": call_id_str,
                "transcript_id": transcript_id,
                "language": whisper_result.language,
                "text_length": len(cleaned_text),
                "segments": len(whisper_result.segments),
                "confidence": whisper_result.confidence_score,
                "cost_usd": whisper_result.cost_usd,
                "processing_time_ms": total_time_ms,
            }

        except Exception as e:
            logger.exception(
                "transcription_pipeline_failed",
                call_id=call_id_str,
                error=str(e),
            )

            # Record failure
            async with get_db_session() as session:
                call_repo = CallRepository(session)
                await call_repo.update_status(
                    call_id_str,
                    status="transcription_failed",
                    error_message=str(e)[:500],
                )

                # Also create a failed transcript record for tracking
                transcript_repo = TranscriptRepository(session)
                await transcript_repo.create(
                    call_id=call_id_str,
                    content="",
                    language=self._settings.whisper_language,
                    model_name=self._settings.whisper_model,
                    status="failed",
                    metadata={"error": str(e)[:500]},
                )
                await session.commit()

            return {
                "status": "error",
                "call_id": call_id_str,
                "error": str(e),
                "retryable": not isinstance(e, TranscriptionError)
                or e.retryable,
            }

        finally:
            # ── Cleanup temp file ────────────────────────────────────────
            if temp_path and self._settings.audio_cleanup_after_upload:
                try:
                    if temp_path.exists():
                        temp_path.unlink()
                        logger.info("transcription_temp_cleaned", path=str(temp_path))
                except Exception:
                    pass

    # ── Audio file retrieval ─────────────────────────────────────────────

    async def _get_audio_file(
        self,
        *,
        call_id: str,
        recording_url: str | None,
        drive_file_id: str | None,
    ) -> Path:
        """
        Get the audio file for transcription.

        Priority:
        1. Download from Google Drive (if drive_file_id exists)
        2. Download from original recording URL

        Returns path to a temporary local file.
        """
        import httpx

        temp_dir = Path(self._settings.audio_temp_dir)
        temp_dir.mkdir(parents=True, exist_ok=True)

        # Option 1: Download from Google Drive
        if drive_file_id:
            try:
                return await self._download_from_drive(drive_file_id, temp_dir, call_id)
            except Exception as e:
                logger.warning(
                    "transcription_drive_download_failed",
                    call_id=call_id,
                    drive_file_id=drive_file_id,
                    error=str(e),
                )
                # Fall through to recording_url

        # Option 2: Download from original URL
        if recording_url:
            return await self._download_from_url(recording_url, temp_dir, call_id)

        raise TranscriptionError(
            "No audio source available",
            call_id=call_id,
            retryable=False,
        )

    async def _download_from_drive(
        self,
        file_id: str,
        temp_dir: Path,
        call_id: str,
    ) -> Path:
        """Download audio from Google Drive via the Drive API."""
        from app.integrations.google_drive import GoogleDriveClient

        drive = GoogleDriveClient()

        # Get file metadata to determine extension
        loop = asyncio.get_running_loop()
        meta = await loop.run_in_executor(None, drive._verify_sync, file_id)

        if not meta.get("exists"):
            raise TranscriptionError(
                f"Drive file {file_id} not found",
                call_id=call_id,
                retryable=False,
            )

        # Determine extension from MIME type
        mime_to_ext = {
            "audio/wav": ".wav",
            "audio/mpeg": ".mp3",
            "audio/ogg": ".ogg",
            "audio/flac": ".flac",
            "audio/mp4": ".m4a",
        }
        ext = mime_to_ext.get(meta.get("mime_type", ""), ".wav")

        # Download the file
        fd, temp_path_str = tempfile.mkstemp(
            prefix=f"whisper-{call_id}-",
            suffix=ext,
            dir=str(temp_dir),
        )
        os.close(fd)
        temp_path = Path(temp_path_str)

        def _download():
            service = drive._get_service()
            request = service.files().get_media(fileId=file_id)
            with open(temp_path, "wb") as f:
                from googleapiclient.http import MediaIoBaseDownload
                import io

                downloader = MediaIoBaseDownload(f, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()

        await loop.run_in_executor(None, _download)

        logger.info(
            "transcription_drive_downloaded",
            call_id=call_id,
            file_id=file_id,
            path=str(temp_path),
            size_bytes=temp_path.stat().st_size,
        )
        return temp_path

    async def _download_from_url(
        self,
        url: str,
        temp_dir: Path,
        call_id: str,
    ) -> Path:
        """Download audio from a URL (fallback to original recording URL)."""
        import httpx

        ext = ".wav"
        for e in (".wav", ".mp3", ".ogg", ".flac", ".m4a"):
            if e in url.lower():
                ext = e
                break

        fd, temp_path_str = tempfile.mkstemp(
            prefix=f"whisper-{call_id}-",
            suffix=ext,
            dir=str(temp_dir),
        )
        os.close(fd)
        temp_path = Path(temp_path_str)

        timeout = httpx.Timeout(
            connect=10.0,
            read=float(self._settings.audio_download_timeout),
        )

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                with open(temp_path, "wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        f.write(chunk)

        logger.info(
            "transcription_url_downloaded",
            call_id=call_id,
            url=url[:100],
            path=str(temp_path),
            size_bytes=temp_path.stat().st_size,
        )
        return temp_path

    # ── Text post-processing ─────────────────────────────────────────────

    def _post_process_transcript(self, text: str) -> str:
        """
        Clean and normalize transcript text.

        Applied transformations:
        - Strip leading/trailing whitespace
        - Normalize excessive whitespace
        - Fix common Whisper artifacts
        - Preserve paragraph structure
        """
        if not text:
            return ""

        cleaned = text.strip()

        # Normalize multiple spaces to single space
        cleaned = re.sub(r" {2,}", " ", cleaned)

        # Normalize multiple newlines to max 2
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)

        # Remove common Whisper hallucination patterns
        hallucination_patterns = [
            r"(?i)bedankt voor het kijken\.?\s*$",   # "Thanks for watching"
            r"(?i)ondertiteling door\.?\s*$",         # "Subtitled by"
            r"(?i)copyright.*$",
            r"(?i)^\.{3,}\s*",                       # Leading dots
        ]
        for pattern in hallucination_patterns:
            cleaned = re.sub(pattern, "", cleaned)

        return cleaned.strip()

    def _build_prompt(self, call: dict) -> str:
        """
        Build a context prompt for Whisper based on call metadata.

        Domain-specific terms and names improve transcription accuracy.
        """
        parts = []

        # Call center context
        parts.append(
            "Dit is een opname van een Nederlands telefoongesprek "
            "bij een klantenservice."
        )

        # Caller/callee info
        caller = call.get("caller_name")
        callee = call.get("callee_name")
        if caller:
            parts.append(f"De beller is {caller}.")
        if callee:
            parts.append(f"De medewerker is {callee}.")

        return " ".join(parts)

    # ── Segment serialization ────────────────────────────────────────────

    def _serialize_segments(self, result: TranscriptionResult) -> list[dict]:
        """Convert TranscriptSegment objects to JSON-serializable dicts."""
        return [
            {
                "id": seg.id,
                "start": round(seg.start, 3),
                "end": round(seg.end, 3),
                "text": seg.text,
                "avg_logprob": round(seg.avg_logprob, 4) if seg.avg_logprob is not None else None,
                "no_speech_prob": round(seg.no_speech_prob, 4) if seg.no_speech_prob is not None else None,
            }
            for seg in result.segments
        ]

    # ── Batch operations ─────────────────────────────────────────────────

    async def transcribe_pending(self, *, batch_size: int = 5) -> list[dict]:
        """
        Transcribe calls in 'transcribing' status (waiting for transcription).

        Picks up calls that were uploaded to Drive but haven't been
        transcribed yet.
        """
        async with get_db_session() as session:
            call_repo = CallRepository(session)
            pending = await call_repo.list_by_status(
                "transcribing", limit=batch_size
            )

        if not pending:
            logger.info("transcription_batch_no_pending")
            return []

        logger.info("transcription_batch_found", count=len(pending))

        results = []
        for call in pending:
            result = await self.transcribe_call(str(call["id"]))
            results.append(result)
            # Rate limit: ~1 call per 2 seconds
            await asyncio.sleep(2)

        succeeded = sum(1 for r in results if r.get("status") == "transcribed")
        failed = sum(1 for r in results if r.get("status") == "error")

        logger.info(
            "transcription_batch_complete",
            total=len(results),
            succeeded=succeeded,
            failed=failed,
            total_cost=round(
                sum(r.get("cost_usd", 0) for r in results), 4
            ),
        )
        return results

    async def retry_failed(self, *, batch_size: int = 3) -> list[dict]:
        """Retry transcriptions that previously failed."""
        async with get_db_session() as session:
            call_repo = CallRepository(session)
            failed = await call_repo.list_by_status(
                "transcription_failed", limit=batch_size
            )

        if not failed:
            return []

        logger.info("transcription_retry_found", count=len(failed))

        results = []
        for call in failed:
            # Reset status for re-processing
            async with get_db_session() as session:
                call_repo = CallRepository(session)
                await call_repo.update_status(
                    str(call["id"]), status="transcribing"
                )
                await session.commit()

            result = await self.transcribe_call(str(call["id"]))
            results.append(result)
            await asyncio.sleep(3)

        return results
