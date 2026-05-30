"""
OpenAI Whisper API integration — audio transcription.

Uses the OpenAI Whisper API (not local Whisper) for cost-effective,
scalable transcription. Handles:
- Single file transcription
- Large file chunking (>25MB)
- Segment-level timestamps
- Language detection
- Retry with exponential backoff
- Cost estimation

All OpenAI API calls run in a thread executor to avoid blocking
the async event loop.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from openai import OpenAI

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class TranscriptSegment:
    """A single timed segment of the transcript."""

    id: int
    start: float       # seconds
    end: float          # seconds
    text: str
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    temperature: float | None = None


@dataclass
class TranscriptionResult:
    """Complete result from a Whisper transcription."""

    text: str                          # full transcript text
    language: str                      # detected or specified language
    duration_seconds: float            # audio duration
    segments: list[TranscriptSegment]  # timed segments
    model: str                         # model used
    model_version: str | None = None
    confidence_score: float | None = None   # avg confidence (from logprobs)
    processing_time_ms: int = 0
    cost_usd: float = 0.0
    chunk_count: int = 1               # how many chunks were processed
    warnings: list[str] = field(default_factory=list)


class WhisperClient:
    """
    Async-compatible OpenAI Whisper API client.

    Handles file size limits by chunking, provides retry logic,
    and estimates costs.
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._client: OpenAI | None = None

    def _get_client(self) -> OpenAI:
        """Lazily initialise the OpenAI client."""
        if self._client is not None:
            return self._client

        api_key = self._settings.openai_api_key
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not configured. "
                "Set it in .env to use Whisper transcription."
            )

        self._client = OpenAI(api_key=api_key)
        logger.info(
            "whisper_client_initialized",
            model=self._settings.whisper_model,
            language=self._settings.whisper_language,
        )
        return self._client

    # ── Public API ───────────────────────────────────────────────────────

    async def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        prompt: str | None = None,
    ) -> TranscriptionResult:
        """
        Transcribe an audio file using OpenAI Whisper API.

        For files under 25MB, sends directly. For larger files,
        splits into chunks and merges results.

        Args:
            audio_path: Path to the audio file
            language: ISO-639-1 language code (defaults to config)
            prompt: Optional context prompt to guide transcription

        Returns:
            TranscriptionResult with full text, segments, and metadata
        """
        if not audio_path.exists():
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        file_size_mb = audio_path.stat().st_size / (1024 * 1024)
        max_size_mb = self._settings.whisper_max_file_size_mb
        lang = language or self._settings.whisper_language

        logger.info(
            "whisper_transcribe_start",
            path=str(audio_path),
            file_size_mb=round(file_size_mb, 2),
            language=lang,
            model=self._settings.whisper_model,
        )

        start_time = time.perf_counter()

        if file_size_mb <= max_size_mb:
            # Direct transcription
            result = await self._transcribe_single(
                audio_path, language=lang, prompt=prompt
            )
        else:
            # Chunk and merge
            logger.info(
                "whisper_chunking_required",
                file_size_mb=round(file_size_mb, 2),
                max_size_mb=max_size_mb,
            )
            result = await self._transcribe_chunked(
                audio_path, language=lang, prompt=prompt
            )

        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        result.processing_time_ms = elapsed_ms

        # Estimate cost
        result.cost_usd = self._estimate_cost(result.duration_seconds)

        logger.info(
            "whisper_transcribe_complete",
            language=result.language,
            duration_seconds=round(result.duration_seconds, 1),
            segments=len(result.segments),
            text_length=len(result.text),
            processing_time_ms=elapsed_ms,
            cost_usd=round(result.cost_usd, 4),
            confidence=round(result.confidence_score, 4) if result.confidence_score else None,
            chunks=result.chunk_count,
        )

        return result

    # ── Single file transcription ────────────────────────────────────────

    async def _transcribe_single(
        self,
        audio_path: Path,
        *,
        language: str,
        prompt: str | None = None,
    ) -> TranscriptionResult:
        """Transcribe a single file (≤25MB) via the OpenAI API."""
        max_attempts = self._settings.whisper_retry_max_attempts
        base_delay = self._settings.whisper_retry_base_delay
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                loop = asyncio.get_running_loop()
                response = await loop.run_in_executor(
                    None,
                    self._call_whisper_api,
                    audio_path,
                    language,
                    prompt,
                )
                return self._parse_response(response)

            except Exception as e:
                last_error = e
                if attempt >= max_attempts:
                    break

                delay = base_delay * (2 ** (attempt - 1))
                logger.warning(
                    "whisper_retry",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    delay_seconds=delay,
                    error=str(e),
                )
                await asyncio.sleep(delay)

        raise RuntimeError(
            f"Whisper transcription failed after {max_attempts} attempts: {last_error}"
        ) from last_error

    def _call_whisper_api(
        self,
        audio_path: Path,
        language: str,
        prompt: str | None,
    ) -> object:
        """
        Synchronous Whisper API call — runs in thread executor.

        Uses verbose_json format to get segment-level timestamps.
        """
        client = self._get_client()

        with open(audio_path, "rb") as audio_file:
            kwargs = {
                "model": self._settings.whisper_model,
                "file": audio_file,
                "response_format": self._settings.whisper_response_format,
                "temperature": self._settings.whisper_temperature,
                "timestamp_granularities": ["segment"],
            }

            # Language hint (None = auto-detect)
            if language and language != "auto":
                kwargs["language"] = language

            # Prompting for domain-specific terms
            if prompt:
                kwargs["prompt"] = prompt

            response = client.audio.transcriptions.create(**kwargs)

        return response

    # ── Chunked transcription ────────────────────────────────────────────

    async def _transcribe_chunked(
        self,
        audio_path: Path,
        *,
        language: str,
        prompt: str | None = None,
    ) -> TranscriptionResult:
        """
        Split a large audio file into chunks and transcribe each.

        Uses ffmpeg to split the file, transcribes each chunk,
        then merges the results with corrected timestamps.
        """
        chunk_dir = audio_path.parent / f"{audio_path.stem}_chunks"
        chunk_dir.mkdir(exist_ok=True)

        try:
            # Split audio into chunks
            chunk_paths = await self._split_audio(audio_path, chunk_dir)
            logger.info(
                "whisper_chunks_created",
                count=len(chunk_paths),
                chunk_dir=str(chunk_dir),
            )

            # Transcribe each chunk sequentially (to manage API rate limits)
            all_segments: list[TranscriptSegment] = []
            all_texts: list[str] = []
            total_duration = 0.0
            time_offset = 0.0
            warnings: list[str] = []

            for i, chunk_path in enumerate(chunk_paths):
                logger.info(
                    "whisper_chunk_transcribing",
                    chunk=i + 1,
                    total=len(chunk_paths),
                    path=str(chunk_path),
                )

                chunk_result = await self._transcribe_single(
                    chunk_path,
                    language=language,
                    prompt=prompt,
                )

                # Offset segment timestamps
                for seg in chunk_result.segments:
                    seg.start += time_offset
                    seg.end += time_offset
                    all_segments.append(seg)

                all_texts.append(chunk_result.text)
                total_duration += chunk_result.duration_seconds
                time_offset += chunk_result.duration_seconds
                warnings.extend(chunk_result.warnings)

                # Small delay between chunks to respect rate limits
                if i < len(chunk_paths) - 1:
                    await asyncio.sleep(1)

            # Compute average confidence
            logprobs = [
                s.avg_logprob for s in all_segments
                if s.avg_logprob is not None
            ]
            avg_confidence = None
            if logprobs:
                import math
                avg_confidence = math.exp(sum(logprobs) / len(logprobs))

            return TranscriptionResult(
                text=" ".join(all_texts),
                language=language,
                duration_seconds=total_duration,
                segments=all_segments,
                model=self._settings.whisper_model,
                confidence_score=avg_confidence,
                chunk_count=len(chunk_paths),
                warnings=warnings,
            )

        finally:
            # Clean up chunk files
            import shutil
            shutil.rmtree(chunk_dir, ignore_errors=True)

    async def _split_audio(
        self,
        audio_path: Path,
        output_dir: Path,
    ) -> list[Path]:
        """
        Split audio into chunks using ffmpeg.

        Each chunk is the configured duration (default: 10 minutes).
        Falls back to single-file processing if ffmpeg is unavailable.
        """
        chunk_minutes = self._settings.whisper_chunk_duration_minutes
        chunk_seconds = chunk_minutes * 60

        try:
            # Use ffmpeg to split
            output_pattern = str(output_dir / f"chunk_%03d{audio_path.suffix}")
            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-i", str(audio_path),
                "-f", "segment",
                "-segment_time", str(chunk_seconds),
                "-c", "copy",
                "-reset_timestamps", "1",
                "-loglevel", "error",
                output_pattern,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()

            if process.returncode != 0:
                logger.warning(
                    "ffmpeg_split_failed",
                    error=stderr.decode("utf-8", errors="replace"),
                )
                # Fallback: return original file
                return [audio_path]

            # Collect chunk files in order
            chunks = sorted(output_dir.glob(f"chunk_*{audio_path.suffix}"))
            if not chunks:
                return [audio_path]

            return chunks

        except FileNotFoundError:
            logger.warning(
                "ffmpeg_not_found",
                message="ffmpeg not installed, sending full file to API",
            )
            return [audio_path]

    # ── Response parsing ─────────────────────────────────────────────────

    def _parse_response(self, response: object) -> TranscriptionResult:
        """Parse the OpenAI Whisper API response into our data model."""
        import math

        # Handle verbose_json response
        text = getattr(response, "text", "")
        language = getattr(response, "language", self._settings.whisper_language)
        duration = getattr(response, "duration", 0.0) or 0.0
        raw_segments = getattr(response, "segments", []) or []

        segments: list[TranscriptSegment] = []
        logprobs: list[float] = []

        for seg in raw_segments:
            # Handle both dict and object segment formats
            if isinstance(seg, dict):
                s = TranscriptSegment(
                    id=seg.get("id", len(segments)),
                    start=seg.get("start", 0.0),
                    end=seg.get("end", 0.0),
                    text=seg.get("text", "").strip(),
                    avg_logprob=seg.get("avg_logprob"),
                    no_speech_prob=seg.get("no_speech_prob"),
                    temperature=seg.get("temperature"),
                )
            else:
                s = TranscriptSegment(
                    id=getattr(seg, "id", len(segments)),
                    start=getattr(seg, "start", 0.0),
                    end=getattr(seg, "end", 0.0),
                    text=getattr(seg, "text", "").strip(),
                    avg_logprob=getattr(seg, "avg_logprob", None),
                    no_speech_prob=getattr(seg, "no_speech_prob", None),
                    temperature=getattr(seg, "temperature", None),
                )

            if s.text:
                segments.append(s)
                if s.avg_logprob is not None:
                    logprobs.append(s.avg_logprob)

        # Calculate average confidence from log probabilities
        avg_confidence = None
        if logprobs:
            avg_confidence = math.exp(sum(logprobs) / len(logprobs))

        warnings: list[str] = []

        # Flag low-confidence segments
        for seg in segments:
            if seg.no_speech_prob and seg.no_speech_prob > 0.8:
                warnings.append(
                    f"Segment {seg.id} ({seg.start:.1f}s-{seg.end:.1f}s) "
                    f"has high no_speech_prob: {seg.no_speech_prob:.3f}"
                )

        return TranscriptionResult(
            text=text.strip(),
            language=language,
            duration_seconds=duration,
            segments=segments,
            model=self._settings.whisper_model,
            confidence_score=avg_confidence,
            warnings=warnings,
        )

    # ── Cost estimation ──────────────────────────────────────────────────

    def _estimate_cost(self, duration_seconds: float) -> float:
        """
        Estimate the API cost for a transcription.

        OpenAI Whisper pricing: $0.006 per minute (as of 2024).
        """
        minutes = duration_seconds / 60.0
        return round(minutes * self._settings.whisper_cost_per_minute, 6)
