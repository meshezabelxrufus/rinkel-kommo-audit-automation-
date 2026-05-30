"""
Audio processing service — download, upload, and manage call recordings.

Orchestrates:
1. Download audio from Rinkel recording URL
2. Validate the downloaded file
3. Upload to Google Drive with folder structure
4. Verify the upload
5. Update call record with Drive metadata
6. Clean up temporary files

Includes:
- Exponential backoff retry logic
- Secure temp file handling
- Size validation
- Comprehensive error recovery
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import httpx

from app.core.config import get_settings
from app.core.database import get_db_session
from app.core.logging import get_logger
from app.integrations.google_drive import DriveUploadResult, GoogleDriveClient
from app.repositories.agent_repository import AgentRepository
from app.repositories.call_repository import CallRepository

logger = get_logger(__name__)


class AudioProcessingError(Exception):
    """Base error for audio processing failures."""

    def __init__(self, message: str, call_id: str, retryable: bool = True) -> None:
        self.call_id = call_id
        self.retryable = retryable
        super().__init__(message)


class AudioDownloadError(AudioProcessingError):
    """Failed to download audio from source URL."""
    pass


class AudioUploadError(AudioProcessingError):
    """Failed to upload audio to Google Drive."""
    pass


class AudioValidationError(AudioProcessingError):
    """Downloaded audio file failed validation."""

    def __init__(self, message: str, call_id: str) -> None:
        super().__init__(message, call_id, retryable=False)


class AudioService:
    """
    Orchestrates the full audio processing lifecycle.

    Usage:
        service = AudioService()
        await service.process_call_audio(call_id="...")
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._drive_client: GoogleDriveClient | None = None
        self._temp_dir = Path(self._settings.audio_temp_dir)

    @property
    def drive_client(self) -> GoogleDriveClient:
        """Lazy-init Drive client."""
        if self._drive_client is None:
            self._drive_client = GoogleDriveClient()
        return self._drive_client

    # ── Main entry point ─────────────────────────────────────────────────

    async def process_call_audio(self, call_id: str | UUID) -> dict:
        """
        Full audio processing pipeline for a single call.

        Steps:
        1. Fetch call record from DB
        2. Download audio from recording_url
        3. Validate downloaded file
        4. Upload to Google Drive
        5. Verify upload
        6. Update call record with Drive metadata
        7. Clean up temp file

        Returns a dict with processing result metadata.
        """
        call_id_str = str(call_id)

        logger.info("audio_pipeline_start", call_id=call_id_str)

        async with get_db_session() as session:
            call_repo = CallRepository(session)
            agent_repo = AgentRepository(session)

            # ── Step 1: Fetch call record ────────────────────────────────
            call = await call_repo.get_by_id(call_id_str)
            if not call:
                logger.error("audio_pipeline_call_not_found", call_id=call_id_str)
                return {"status": "error", "error": "Call not found"}

            recording_url = call.get("recording_url")
            if not recording_url:
                logger.warning("audio_pipeline_no_recording_url", call_id=call_id_str)
                return {"status": "skipped", "reason": "No recording URL"}

            # Skip if already uploaded
            if call.get("audio_drive_file_id"):
                logger.info(
                    "audio_pipeline_already_uploaded",
                    call_id=call_id_str,
                    drive_file_id=call["audio_drive_file_id"],
                )
                return {
                    "status": "already_uploaded",
                    "drive_file_id": call["audio_drive_file_id"],
                }

            # ── Step 2: Update status → downloading ──────────────────────
            await call_repo.update_status(call_id_str, status="downloading")
            await session.commit()

        # ── Step 3: Download with retry ──────────────────────────────────
        temp_path: Path | None = None
        try:
            temp_path = await self._download_with_retry(
                url=recording_url,
                call_id=call_id_str,
            )

            # ── Step 4: Validate ─────────────────────────────────────────
            file_size = self._validate_audio_file(temp_path, call_id_str)

            # ── Step 5: Resolve agent name for folder structure ──────────
            agent_name = None
            async with get_db_session() as session:
                if call.get("agent_id"):
                    agent_repo = AgentRepository(session)
                    agent = await agent_repo.get_by_id(str(call["agent_id"]))
                    if agent:
                        agent_name = agent.get("display_name")

            # ── Step 6: Upload status + upload to Drive ──────────────────
            async with get_db_session() as session:
                call_repo = CallRepository(session)
                await call_repo.update_status(call_id_str, status="uploading")
                await session.commit()

            call_date = call.get("started_at") or call.get("created_at")
            if isinstance(call_date, str):
                call_date = datetime.fromisoformat(call_date)

            upload_result = await self._upload_with_retry(
                file_path=temp_path,
                call_id=call.get("external_call_id", call_id_str),
                agent_name=agent_name,
                call_date=call_date,
            )

            # ── Step 7: Verify upload ────────────────────────────────────
            verification = await self.drive_client.verify_upload(upload_result.file_id)
            if not verification.get("exists"):
                raise AudioUploadError(
                    f"Upload verification failed for {upload_result.file_id}",
                    call_id=call_id_str,
                )

            logger.info(
                "audio_upload_verified",
                call_id=call_id_str,
                file_id=upload_result.file_id,
                verified_size=verification.get("size"),
            )

            # ── Step 8: Update call record ───────────────────────────────
            async with get_db_session() as session:
                call_repo = CallRepository(session)
                await call_repo.update_audio_metadata(
                    call_id_str,
                    audio_drive_file_id=upload_result.file_id,
                    audio_drive_url=upload_result.web_view_link,
                    audio_size_bytes=upload_result.size_bytes,
                    status="transcribing",
                )
                await session.commit()

            logger.info(
                "audio_pipeline_complete",
                call_id=call_id_str,
                drive_file_id=upload_result.file_id,
                file_size_bytes=file_size,
            )

            return {
                "status": "uploaded",
                "call_id": call_id_str,
                "drive_file_id": upload_result.file_id,
                "drive_url": upload_result.web_view_link,
                "size_bytes": upload_result.size_bytes,
            }

        except AudioValidationError as e:
            logger.error(
                "audio_pipeline_validation_failed",
                call_id=call_id_str,
                error=str(e),
            )
            async with get_db_session() as session:
                call_repo = CallRepository(session)
                await call_repo.update_status(
                    call_id_str,
                    status="download_failed",
                    error_message=str(e),
                )
                await session.commit()
            return {"status": "error", "error": str(e), "retryable": False}

        except AudioDownloadError as e:
            logger.error(
                "audio_pipeline_download_failed",
                call_id=call_id_str,
                error=str(e),
            )
            async with get_db_session() as session:
                call_repo = CallRepository(session)
                await call_repo.update_status(
                    call_id_str,
                    status="download_failed",
                    error_message=str(e),
                )
                await session.commit()
            return {"status": "error", "error": str(e), "retryable": e.retryable}

        except AudioUploadError as e:
            logger.error(
                "audio_pipeline_upload_failed",
                call_id=call_id_str,
                error=str(e),
            )
            async with get_db_session() as session:
                call_repo = CallRepository(session)
                await call_repo.update_status(
                    call_id_str,
                    status="upload_failed",
                    error_message=str(e),
                )
                await session.commit()
            return {"status": "error", "error": str(e), "retryable": e.retryable}

        except Exception as e:
            logger.exception(
                "audio_pipeline_unexpected_error",
                call_id=call_id_str,
                error=str(e),
            )
            async with get_db_session() as session:
                call_repo = CallRepository(session)
                await call_repo.update_status(
                    call_id_str,
                    status="download_failed",
                    error_message=f"Unexpected error: {e}",
                )
                await session.commit()
            return {"status": "error", "error": str(e), "retryable": True}

        finally:
            # ── Step 9: Cleanup temp file ────────────────────────────────
            if temp_path and self._settings.audio_cleanup_after_upload:
                self._cleanup_temp_file(temp_path)

    # ── Download ─────────────────────────────────────────────────────────

    async def _download_with_retry(
        self,
        url: str,
        call_id: str,
    ) -> Path:
        """
        Download audio with exponential backoff retry.

        Returns the path to the downloaded temp file.
        """
        max_attempts = self._settings.audio_retry_max_attempts
        base_delay = self._settings.audio_retry_base_delay
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                return await self._download_audio(url, call_id)
            except AudioDownloadError as e:
                last_error = e
                if attempt >= max_attempts:
                    break
                if not e.retryable:
                    raise

                delay = base_delay * (2 ** (attempt - 1))  # 2s, 4s, 8s
                logger.warning(
                    "audio_download_retry",
                    call_id=call_id,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    delay_seconds=delay,
                    error=str(e),
                )
                await asyncio.sleep(delay)

        raise last_error or AudioDownloadError(
            "Download failed after all retries", call_id=call_id
        )

    async def _download_audio(self, url: str, call_id: str) -> Path:
        """
        Download an audio file from URL to a secure temp file.

        Uses streaming download to handle large files without
        loading them entirely into memory.
        """
        # Ensure temp directory exists with restricted permissions
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(str(self._temp_dir), 0o700)

        # Create a uniquely named temp file
        suffix = self._guess_extension(url)
        fd, temp_path_str = tempfile.mkstemp(
            prefix=f"rinkel-{call_id}-",
            suffix=suffix,
            dir=str(self._temp_dir),
        )
        os.close(fd)
        temp_path = Path(temp_path_str)

        timeout = httpx.Timeout(
            connect=10.0,
            read=float(self._settings.audio_download_timeout),
            write=30.0,
            pool=10.0,
        )
        max_size = self._settings.audio_max_file_size_mb * 1024 * 1024

        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                http2=False,
            ) as client:
                async with client.stream("GET", url) as response:
                    # Check HTTP status
                    if response.status_code == 404:
                        raise AudioDownloadError(
                            f"Recording not found (404): {url}",
                            call_id=call_id,
                            retryable=False,
                        )
                    if response.status_code == 403:
                        raise AudioDownloadError(
                            f"Recording access denied (403): {url}",
                            call_id=call_id,
                            retryable=False,
                        )
                    if response.status_code >= 500:
                        raise AudioDownloadError(
                            f"Server error ({response.status_code}): {url}",
                            call_id=call_id,
                            retryable=True,
                        )
                    response.raise_for_status()

                    # Check content-length if available
                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > max_size:
                        raise AudioValidationError(
                            f"File too large: {int(content_length)} bytes "
                            f"(max {max_size} bytes)",
                            call_id=call_id,
                        )

                    # Stream to disk
                    downloaded_bytes = 0
                    with open(temp_path, "wb") as f:
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            downloaded_bytes += len(chunk)
                            if downloaded_bytes > max_size:
                                raise AudioValidationError(
                                    f"File exceeds max size during download: "
                                    f"{downloaded_bytes} bytes (max {max_size})",
                                    call_id=call_id,
                                )
                            f.write(chunk)

            logger.info(
                "audio_downloaded",
                call_id=call_id,
                path=str(temp_path),
                size_bytes=downloaded_bytes,
            )
            return temp_path

        except (AudioDownloadError, AudioValidationError):
            # Clean up on known errors
            self._cleanup_temp_file(temp_path)
            raise

        except httpx.TimeoutException as e:
            self._cleanup_temp_file(temp_path)
            raise AudioDownloadError(
                f"Download timed out: {e}", call_id=call_id, retryable=True
            ) from e

        except httpx.HTTPStatusError as e:
            self._cleanup_temp_file(temp_path)
            retryable = e.response.status_code >= 500
            raise AudioDownloadError(
                f"HTTP error {e.response.status_code}: {e}",
                call_id=call_id,
                retryable=retryable,
            ) from e

        except Exception as e:
            self._cleanup_temp_file(temp_path)
            raise AudioDownloadError(
                f"Download failed: {e}", call_id=call_id, retryable=True
            ) from e

    # ── Upload ───────────────────────────────────────────────────────────

    async def _upload_with_retry(
        self,
        file_path: Path,
        call_id: str,
        agent_name: str | None,
        call_date: datetime | None,
    ) -> DriveUploadResult:
        """Upload to Drive with exponential backoff retry."""
        max_attempts = self._settings.audio_retry_max_attempts
        base_delay = self._settings.audio_retry_base_delay
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                return await self.drive_client.upload_audio(
                    file_path,
                    call_id=call_id,
                    agent_name=agent_name,
                    call_date=call_date,
                )
            except Exception as e:
                last_error = e
                if attempt >= max_attempts:
                    break

                delay = base_delay * (2 ** (attempt - 1))
                logger.warning(
                    "audio_upload_retry",
                    call_id=call_id,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    delay_seconds=delay,
                    error=str(e),
                )
                await asyncio.sleep(delay)

        raise AudioUploadError(
            f"Drive upload failed after {max_attempts} attempts: {last_error}",
            call_id=call_id,
        )

    # ── Validation ───────────────────────────────────────────────────────

    def _validate_audio_file(self, file_path: Path, call_id: str) -> int:
        """
        Validate a downloaded audio file.

        Checks:
        - File exists
        - File is not empty
        - File is within size limits
        - File has a valid audio extension

        Returns file size in bytes.
        """
        if not file_path.exists():
            raise AudioValidationError(
                f"Downloaded file not found: {file_path}", call_id=call_id
            )

        file_size = file_path.stat().st_size
        if file_size == 0:
            raise AudioValidationError(
                "Downloaded file is empty (0 bytes)", call_id=call_id
            )

        max_size = self._settings.audio_max_file_size_mb * 1024 * 1024
        if file_size > max_size:
            raise AudioValidationError(
                f"File too large: {file_size} bytes (max {max_size})",
                call_id=call_id,
            )

        # Basic audio file check
        valid_extensions = {".wav", ".mp3", ".ogg", ".flac", ".m4a", ".webm"}
        if file_path.suffix.lower() not in valid_extensions:
            logger.warning(
                "audio_unexpected_extension",
                call_id=call_id,
                extension=file_path.suffix,
            )

        logger.info(
            "audio_validated",
            call_id=call_id,
            size_bytes=file_size,
            extension=file_path.suffix,
        )
        return file_size

    # ── Cleanup ──────────────────────────────────────────────────────────

    def _cleanup_temp_file(self, file_path: Path) -> None:
        """Securely delete a temporary file."""
        try:
            if file_path.exists():
                file_path.unlink()
                logger.info("audio_temp_cleaned", path=str(file_path))
        except Exception as e:
            logger.warning(
                "audio_temp_cleanup_failed",
                path=str(file_path),
                error=str(e),
            )

    async def cleanup_stale_temp_files(self, max_age_hours: int = 24) -> int:
        """
        Remove temp audio files older than max_age_hours.

        Run periodically to prevent disk space leaks from
        failed processing runs.

        Returns the number of files cleaned.
        """
        if not self._temp_dir.exists():
            return 0

        import time

        cutoff = time.time() - (max_age_hours * 3600)
        cleaned = 0

        for f in self._temp_dir.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                try:
                    f.unlink()
                    cleaned += 1
                    logger.info("stale_temp_cleaned", path=str(f))
                except Exception as e:
                    logger.warning(
                        "stale_temp_cleanup_failed",
                        path=str(f),
                        error=str(e),
                    )

        if cleaned:
            logger.info("stale_temp_cleanup_complete", files_cleaned=cleaned)

        return cleaned

    # ── Utility ──────────────────────────────────────────────────────────

    @staticmethod
    def _guess_extension(url: str) -> str:
        """Guess file extension from URL."""
        from urllib.parse import urlparse

        path = urlparse(url).path.lower()
        for ext in (".wav", ".mp3", ".ogg", ".flac", ".m4a", ".webm"):
            if path.endswith(ext):
                return ext
        return ".wav"  # default
