"""
Rinkel webhook router — production ingestion endpoint.

Handles:
- POST /api/v1/webhooks/rinkel — main webhook receiver
- GET  /api/v1/webhooks/rinkel — health check / verification handshake

Features:
- Raw body capture for signature verification
- Structured request metadata extraction
- Idempotent processing via WebhookService
- Comprehensive error handling with proper HTTP status codes
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse

from app.core.database import get_db_session
from app.core.logging import get_logger
from app.models.schemas import RinkelWebhookPayload, WebhookResponse
from app.services.webhook_service import WebhookService

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = get_logger(__name__)


@router.post(
    "/rinkel",
    summary="Rinkel call webhook receiver",
    description=(
        "Receives call event webhooks from Rinkel. "
        "Supports idempotent re-delivery and signature verification."
    ),
    response_model=WebhookResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        202: {"description": "Webhook accepted and queued for processing"},
        200: {"description": "Duplicate webhook — already processed"},
        400: {"description": "Invalid payload format"},
        401: {"description": "Invalid webhook signature"},
        500: {"description": "Internal processing error"},
    },
)
async def rinkel_webhook(request: Request) -> Response:
    """
    Process an incoming Rinkel call webhook.

    The endpoint:
    1. Reads the raw body for signature verification
    2. Parses the JSON into a RinkelWebhookPayload
    3. Delegates to WebhookService for full pipeline processing
    4. Returns appropriate HTTP status based on outcome
    """
    # ── Read raw body (needed for signature verification) ────────────────
    raw_body = await request.body()

    if not raw_body:
        logger.warning("webhook_empty_body", path=str(request.url))
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"status": "error", "error": "Empty request body"},
        )

    # ── Parse payload ────────────────────────────────────────────────────
    try:
        payload_dict = await request.json()
        payload = RinkelWebhookPayload.model_validate(payload_dict)
    except Exception as e:
        logger.error(
            "webhook_parse_error",
            error=str(e),
            body_preview=raw_body[:500].decode("utf-8", errors="replace"),
        )
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "status": "error",
                "error": "Invalid JSON payload",
                "detail": str(e),
            },
        )

    # ── Extract request metadata ─────────────────────────────────────────
    headers = dict(request.headers)
    signature = (
        headers.get("x-rinkel-signature")
        or headers.get("x-webhook-signature")
        or headers.get("x-signature")
    )
    ip_address = request.client.host if request.client else None
    user_agent = headers.get("user-agent")

    # ── Process through service layer ────────────────────────────────────
    try:
        async with get_db_session() as session:
            service = WebhookService(session)
            result = await service.process_rinkel_webhook(
                payload=payload,
                raw_body=raw_body,
                headers=headers,
                signature=signature,
                ip_address=ip_address,
                user_agent=user_agent,
            )
    except Exception as e:
        logger.exception(
            "webhook_processing_failed",
            error=str(e),
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "status": "error",
                "error": "Internal processing error",
            },
        )

    # ── Return appropriate status code ───────────────────────────────────
    if result.status == "duplicate":
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=result.model_dump(),
        )

    if result.status == "error":
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=result.model_dump(),
        )

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=result.model_dump(),
    )


@router.get(
    "/rinkel",
    summary="Webhook verification handshake",
    description="Returns 200 OK for webhook URL verification by Rinkel.",
)
async def rinkel_webhook_verify() -> dict:
    """
    Verification endpoint for Rinkel webhook registration.

    Some webhook providers send a GET request to verify the URL
    is reachable before enabling delivery.
    """
    return {
        "status": "ok",
        "message": "Rinkel webhook endpoint is active",
    }
