"""
Export API endpoints — JSONL export for Claude auditing.

Endpoints:
- POST   /api/v1/exports              Create export job (file-based)
- POST   /api/v1/exports/stream       Stream JSONL (direct download)
- POST   /api/v1/exports/preview      Preview record count
- GET    /api/v1/exports              List export jobs
- GET    /api/v1/exports/{id}         Get export job status
- GET    /api/v1/exports/{id}/download Download export file
- DELETE /api/v1/exports/{id}         Cancel export job
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from app.core.logging import get_logger
from app.models.export_schemas import (
    CreateExportRequest,
    ExportCountResponse,
    ExportFilters,
    ExportJobListResponse,
    ExportJobResponse,
)
from app.services.export_service import ExportService

logger = get_logger(__name__)

router = APIRouter(prefix="/exports", tags=["exports"])

# ── Create export job ────────────────────────────────────────────────────


@router.post(
    "",
    response_model=ExportJobResponse,
    status_code=202,
    summary="Create JSONL export job",
    description="Create a file-based JSONL export job. The export runs "
    "asynchronously and the file can be downloaded when complete.",
)
async def create_export(request: CreateExportRequest):
    """Create a new JSONL export job."""
    service = ExportService()
    result = await service.create_export(
        job_name=request.job_name,
        filters=request.filters,
    )

    if result.get("status") == "failed":
        raise HTTPException(status_code=500, detail=result.get("error"))

    # Fetch the full job to return
    job = await service.get_job(result["job_id"])
    if not job:
        raise HTTPException(status_code=500, detail="Export job not found after creation")

    return _job_to_response(job)


# ── Stream JSONL ─────────────────────────────────────────────────────────


@router.post(
    "/stream",
    summary="Stream JSONL export",
    description="Stream JSONL records directly as an HTTP response. "
    "Memory-efficient — suitable for large exports. "
    "No job record is created.",
    responses={
        200: {
            "content": {"application/x-ndjson": {}},
            "description": "Streaming JSONL response",
        }
    },
)
async def stream_export(request: CreateExportRequest):
    """Stream JSONL records directly to the client."""
    service = ExportService()

    # Preview count first
    preview = await service.preview(request.filters)
    if preview["count"] == 0:
        raise HTTPException(status_code=404, detail="No records match the filters")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"export_{timestamp}.jsonl"

    return StreamingResponse(
        service.stream_jsonl(request.filters),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Record-Count": str(preview["count"]),
        },
    )


# ── Preview (count without export) ───────────────────────────────────────


@router.post(
    "/preview",
    response_model=ExportCountResponse,
    summary="Preview export count",
    description="Count how many records match the filters without "
    "generating an export. Use to estimate size and cost.",
)
async def preview_export(filters: ExportFilters | None = None):
    """Preview how many records match the export filters."""
    service = ExportService()
    result = await service.preview(filters or ExportFilters())
    return ExportCountResponse(**result)


# ── List export jobs ─────────────────────────────────────────────────────


@router.get(
    "",
    response_model=ExportJobListResponse,
    summary="List export jobs",
)
async def list_exports(
    status: str | None = Query(None, description="Filter by status"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """List export jobs with optional status filter."""
    service = ExportService()
    jobs, total = await service.list_jobs(
        status=status,
        limit=limit,
        offset=offset,
    )
    return ExportJobListResponse(
        jobs=[_job_to_response(j) for j in jobs],
        total=total,
        limit=limit,
        offset=offset,
    )


# ── Get export job ───────────────────────────────────────────────────────


@router.get(
    "/{job_id}",
    response_model=ExportJobResponse,
    summary="Get export job details",
)
async def get_export(job_id: str):
    """Get the status and details of an export job."""
    service = ExportService()
    job = await service.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Export job not found")
    return _job_to_response(job)


# ── Download export file ─────────────────────────────────────────────────


@router.get(
    "/{job_id}/download",
    summary="Download export file",
    description="Download the generated JSONL file. "
    "Only available for completed export jobs.",
    responses={
        200: {
            "content": {"application/x-ndjson": {}},
            "description": "JSONL file download",
        }
    },
)
async def download_export(job_id: str):
    """Download the JSONL file for a completed export job."""
    service = ExportService()
    job = await service.get_job(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Export job not found")

    if job["status"] != "completed":
        raise HTTPException(
            status_code=409,
            detail=f"Export not ready — current status: {job['status']}",
        )

    file_path = job.get("file_path")
    if not file_path or not Path(file_path).exists():
        raise HTTPException(
            status_code=410,
            detail="Export file no longer available (may have been cleaned up)",
        )

    filename = Path(file_path).name

    return FileResponse(
        path=file_path,
        media_type="application/x-ndjson",
        filename=filename,
        headers={
            "X-Record-Count": str(job.get("call_count", 0)),
            "X-File-Checksum": job.get("file_checksum", ""),
        },
    )


# ── Cancel export job ────────────────────────────────────────────────────


@router.delete(
    "/{job_id}",
    summary="Cancel export job",
)
async def cancel_export(job_id: str):
    """Cancel a pending or running export job."""
    service = ExportService()
    result = await service.cancel_job(job_id)
    if not result:
        raise HTTPException(status_code=404, detail="Export job not found")
    return {"status": "cancelled", "job_id": job_id}


# ── Helpers ──────────────────────────────────────────────────────────────


def _job_to_response(job: dict) -> ExportJobResponse:
    """Convert a DB row dict to an ExportJobResponse."""
    job_id = str(job["id"])
    download_url = None
    if job.get("status") == "completed" and job.get("file_path"):
        download_url = f"/api/v1/exports/{job_id}/download"

    return ExportJobResponse(
        id=job_id,
        job_name=job.get("job_name"),
        status=job.get("status", "pending"),
        filters=job.get("filter_criteria") or {},
        call_count=job.get("call_count", 0),
        file_path=job.get("file_path"),
        file_size_bytes=job.get("file_size_bytes"),
        file_checksum=job.get("file_checksum"),
        date_range_start=job.get("date_range_start"),
        date_range_end=job.get("date_range_end"),
        started_at=job.get("started_at"),
        completed_at=job.get("completed_at"),
        processing_time_ms=job.get("processing_time_ms"),
        error_message=job.get("error_message"),
        created_at=job.get("created_at"),
        download_url=download_url,
    )
