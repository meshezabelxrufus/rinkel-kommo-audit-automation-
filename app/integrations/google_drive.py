"""
Google Drive integration — upload audio recordings with folder organization.

Uses a Google service account for server-to-server auth.
Organises files into: year/month/day/agent_name/

All Drive API calls run in a thread executor to avoid blocking
the async event loop (google-api-python-client is synchronous).
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Drive API scopes
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# MIME type mapping
MIME_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".webm": "audio/webm",
}


class DriveUploadResult:
    """Result of a successful Google Drive upload."""

    def __init__(
        self,
        file_id: str,
        file_name: str,
        web_view_link: str,
        web_content_link: str | None,
        size_bytes: int,
        parent_folder_id: str,
    ) -> None:
        self.file_id = file_id
        self.file_name = file_name
        self.web_view_link = web_view_link
        self.web_content_link = web_content_link
        self.size_bytes = size_bytes
        self.parent_folder_id = parent_folder_id

    def __repr__(self) -> str:
        return f"DriveUploadResult(file_id={self.file_id!r}, file_name={self.file_name!r})"


class GoogleDriveClient:
    """
    Async-compatible Google Drive client for audio file uploads.

    Uses service account credentials and organises uploads into
    a date/agent folder hierarchy.
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._root_folder_id = self._settings.google_drive_folder_id
        self._service = None
        self._folder_cache: dict[str, str] = {}  # path → folder_id

    def _get_service(self):
        """Lazily build the Drive API service (synchronous)."""
        if self._service is not None:
            return self._service

        sa_file = self._settings.google_service_account_file
        if not sa_file or not os.path.exists(sa_file):
            raise RuntimeError(
                f"Google service account file not found: {sa_file!r}. "
                "Set GOOGLE_SERVICE_ACCOUNT_FILE in .env"
            )

        credentials = Credentials.from_service_account_file(sa_file, scopes=SCOPES)
        self._service = build("drive", "v3", credentials=credentials, cache_discovery=False)

        logger.info(
            "drive_service_initialized",
            service_account=sa_file,
            root_folder=self._root_folder_id,
        )
        return self._service

    # ── Public API ───────────────────────────────────────────────────────

    async def upload_audio(
        self,
        file_path: Path,
        *,
        call_id: str,
        agent_name: str | None = None,
        call_date: datetime | None = None,
    ) -> DriveUploadResult:
        """
        Upload an audio file to Google Drive with folder organization.

        Folder structure: {root}/YYYY/MM/DD/{agent_name}/
        File name: {call_id}_{original_name}

        Returns a DriveUploadResult with file_id, links, and metadata.
        """
        if not file_path.exists():
            raise FileNotFoundError(f"Audio file not found: {file_path}")

        file_size = file_path.stat().st_size
        if file_size == 0:
            raise ValueError(f"Audio file is empty: {file_path}")

        # Determine folder path
        dt = call_date or datetime.now(timezone.utc)
        agent_folder = _sanitize_folder_name(agent_name or "unknown-agent")
        folder_path = f"{dt.strftime('%Y')}/{dt.strftime('%m')}/{dt.strftime('%d')}/{agent_folder}"

        # Build the target file name
        suffix = file_path.suffix.lower()
        target_name = f"{call_id}{suffix}"
        mime_type = MIME_TYPES.get(suffix, "audio/wav")

        logger.info(
            "drive_upload_start",
            call_id=call_id,
            file=str(file_path),
            file_size_bytes=file_size,
            folder_path=folder_path,
            target_name=target_name,
        )

        # Run synchronous Drive API calls in thread executor
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            self._upload_sync,
            file_path,
            target_name,
            mime_type,
            folder_path,
            file_size,
        )

        logger.info(
            "drive_upload_complete",
            call_id=call_id,
            file_id=result.file_id,
            web_view_link=result.web_view_link,
            size_bytes=result.size_bytes,
        )

        return result

    async def verify_upload(self, file_id: str) -> dict[str, Any]:
        """
        Verify a file exists on Drive and return its metadata.

        Used after upload to confirm the file is accessible.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._verify_sync, file_id)

    async def delete_file(self, file_id: str) -> bool:
        """Delete a file from Drive (for cleanup/recovery)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._delete_sync, file_id)

    # ── Synchronous internals (run in executor) ──────────────────────────

    def _upload_sync(
        self,
        file_path: Path,
        target_name: str,
        mime_type: str,
        folder_path: str,
        file_size: int,
    ) -> DriveUploadResult:
        """Synchronous upload — runs in thread executor."""
        service = self._get_service()

        # Ensure folder hierarchy exists
        parent_id = self._ensure_folder_hierarchy(folder_path)

        # Upload the file
        file_metadata = {
            "name": target_name,
            "parents": [parent_id],
        }

        # Use resumable upload for large files (>5MB)
        resumable = file_size > 5 * 1024 * 1024
        media = MediaFileUpload(
            str(file_path),
            mimetype=mime_type,
            resumable=resumable,
            chunksize=10 * 1024 * 1024,  # 10MB chunks
        )

        uploaded = (
            service.files()
            .create(
                body=file_metadata,
                media_body=media,
                fields="id, name, webViewLink, webContentLink, size",
                supportsAllDrives=True,
            )
            .execute()
        )

        # Auto-share with configured users
        self._auto_share(uploaded["id"])

        return DriveUploadResult(
            file_id=uploaded["id"],
            file_name=uploaded["name"],
            web_view_link=uploaded.get("webViewLink", ""),
            web_content_link=uploaded.get("webContentLink"),
            size_bytes=int(uploaded.get("size", file_size)),
            parent_folder_id=parent_id,
        )

    def _verify_sync(self, file_id: str) -> dict[str, Any]:
        """Synchronous file verification."""
        service = self._get_service()
        try:
            file_meta = (
                service.files()
                .get(
                    fileId=file_id,
                    fields="id, name, mimeType, size, webViewLink, trashed",
                    supportsAllDrives=True,
                )
                .execute()
            )
            return {
                "exists": True,
                "trashed": file_meta.get("trashed", False),
                "file_id": file_meta["id"],
                "name": file_meta["name"],
                "size": int(file_meta.get("size", 0)),
                "mime_type": file_meta.get("mimeType", ""),
            }
        except Exception as e:
            logger.error("drive_verify_failed", file_id=file_id, error=str(e))
            return {"exists": False, "error": str(e)}

    def _delete_sync(self, file_id: str) -> bool:
        """Synchronous file deletion."""
        service = self._get_service()
        try:
            service.files().delete(fileId=file_id, supportsAllDrives=True).execute()
            logger.info("drive_file_deleted", file_id=file_id)
            return True
        except Exception as e:
            logger.error("drive_delete_failed", file_id=file_id, error=str(e))
            return False

    # ── Folder management ────────────────────────────────────────────────

    def _ensure_folder_hierarchy(self, folder_path: str) -> str:
        """
        Create nested folders if they don't exist.

        folder_path: "2026/05/29/jan-de-vries"
        Creates each level and returns the leaf folder ID.
        Uses an in-memory cache to avoid redundant API calls.
        """
        if folder_path in self._folder_cache:
            return self._folder_cache[folder_path]

        service = self._get_service()
        parts = folder_path.strip("/").split("/")
        current_parent = self._root_folder_id

        # Build path incrementally, caching each level
        accumulated_path = ""
        for part in parts:
            accumulated_path = f"{accumulated_path}/{part}" if accumulated_path else part

            if accumulated_path in self._folder_cache:
                current_parent = self._folder_cache[accumulated_path]
                continue

            # Search for existing folder
            folder_id = self._find_folder(service, part, current_parent)

            if folder_id is None:
                # Create the folder
                folder_id = self._create_folder(service, part, current_parent)
                logger.info(
                    "drive_folder_created",
                    name=part,
                    path=accumulated_path,
                    folder_id=folder_id,
                )

            self._folder_cache[accumulated_path] = folder_id
            current_parent = folder_id

        self._folder_cache[folder_path] = current_parent
        return current_parent

    def _find_folder(self, service, name: str, parent_id: str) -> str | None:
        """Find a folder by name within a parent folder."""
        query = (
            f"name = '{name}' "
            f"and '{parent_id}' in parents "
            f"and mimeType = 'application/vnd.google-apps.folder' "
            f"and trashed = false"
        )
        results = (
            service.files()
            .list(
                q=query,
                fields="files(id, name)",
                pageSize=1,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files = results.get("files", [])
        return files[0]["id"] if files else None

    def _create_folder(self, service, name: str, parent_id: str) -> str:
        """Create a folder in Drive."""
        folder_metadata = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        folder = (
            service.files()
            .create(
                body=folder_metadata,
                fields="id",
                supportsAllDrives=True,
            )
            .execute()
        )
        return folder["id"]

    def _auto_share(self, file_id: str) -> None:
        """Share file with configured users (reader access)."""
        share_with = self._settings.google_drive_share_with
        if not share_with:
            return

        service = self._get_service()
        emails = [e.strip() for e in share_with.split(",") if e.strip()]

        for email in emails:
            try:
                service.permissions().create(
                    fileId=file_id,
                    body={
                        "type": "user",
                        "role": "reader",
                        "emailAddress": email,
                    },
                    sendNotificationEmail=False,
                    supportsAllDrives=True,
                ).execute()
            except Exception as e:
                # Non-fatal — log and continue
                logger.warning(
                    "drive_share_failed",
                    file_id=file_id,
                    email=email,
                    error=str(e),
                )


def _sanitize_folder_name(name: str) -> str:
    """Sanitize a string for use as a Drive folder name."""
    # Replace spaces and special chars with hyphens
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in name.lower())
    # Collapse consecutive hyphens
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe.strip("-") or "unknown"
