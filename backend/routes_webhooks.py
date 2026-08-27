"""Webhook routes.

The handler reads the RAW request body. It must never parse-then-re-serialise
before verification: `json.dumps(json.loads(body))` does not reproduce the
original bytes, so the HMAC would never match. There is a test asserting this.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Header, Request, Response, status

from . import db
from .config import get_settings
from .integrations.ingest import (
    ACCEPTED,
    DUPLICATE,
    IGNORED,
    INVALID_SIGNATURE,
    MALFORMED,
    ingest_webhook,
)
from .integrations.webhook import compute_signature

router = APIRouter()


@router.post("/webhooks/razorpay")
async def razorpay_webhook(
    request: Request,
    response: Response,
    x_razorpay_signature: str | None = Header(default=None),
    x_razorpay_event_id: str | None = Header(default=None),
):
    """Receive a Razorpay webhook.

    Always returns 2xx for anything we have durably handled — including
    duplicates — because a non-2xx makes Razorpay retry, and retrying a
    duplicate we already stored achieves nothing. 401 is reserved for a failed
    signature: that is not a delivery problem, and we do want it visible in
    Razorpay's dashboard.
    """
    raw_body = await request.body()
    settings = get_settings()

    async with db.pool().acquire() as conn:
        result, event = await ingest_webhook(
            conn,
            raw_body=raw_body,
            signature=x_razorpay_signature,
            event_id=x_razorpay_event_id,
            secret=settings.razorpay_webhook_secret,
        )

    if result == INVALID_SIGNATURE:
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return {"status": "rejected", "reason": "invalid_signature"}

    if result == MALFORMED:
        # 400, not 500: the payload is wrong, retrying will not fix it.
        response.status_code = status.HTTP_400_BAD_REQUEST
        return {"status": "rejected", "reason": "malformed"}

    return {
        "status": "ok",
        "result": result,
        "event_type": event.event_type if event else None,
        # Explicitly surfaced so duplicate handling is observable in the demo.
        "deduplicated": result == DUPLICATE,
        "actionable": result == ACCEPTED,
        "ignored": result == IGNORED,
    }


@router.post("/dev/replay-webhook")
async def replay_webhook(request: Request, response: Response):
    """Dev-only: replay a webhook payload through the REAL handler.

    This does NOT bypass signature verification. It signs the payload with the
    configured webhook secret and the result is verified exactly like a genuine
    delivery (decision D3). There is one ingest path, not two.
    """
    settings = get_settings()
    if not settings.enable_dev_endpoints:
        response.status_code = status.HTTP_404_NOT_FOUND
        return {"detail": "Not Found"}

    body = await request.json()
    payload = body.get("payload")
    event_id = body.get("event_id")
    if not payload or not event_id:
        response.status_code = status.HTTP_400_BAD_REQUEST
        return {"status": "rejected", "reason": "payload and event_id required"}

    raw = json.dumps(payload).encode()
    signature = compute_signature(raw, settings.razorpay_webhook_secret)

    async with db.pool().acquire() as conn:
        result, event = await ingest_webhook(
            conn,
            raw_body=raw,
            signature=signature,
            event_id=event_id,
            secret=settings.razorpay_webhook_secret,
            source="replay",
        )

    return {
        "status": "ok",
        "result": result,
        "event_type": event.event_type if event else None,
        "deduplicated": result == DUPLICATE,
        "signature_verified": True,
    }
