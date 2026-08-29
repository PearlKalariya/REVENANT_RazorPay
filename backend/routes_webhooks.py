"""Webhook routes.

The handler reads the RAW request body. It must never parse-then-re-serialise
before verification: `json.dumps(json.loads(body))` does not reproduce the
original bytes, so the HMAC would never match. There is a test asserting this.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Header, Request, Response, status

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
from .recovery.outcome import record_outcome
from .security import require_api_key, require_local_dev

router = APIRouter()

#: Razorpay webhook payloads are a few KB. Anything far larger is abuse, not a
#: webhook. `await request.body()` buffers the whole request in memory, so an
#: unbounded POST to this PUBLIC, UNAUTHENTICATED endpoint is a trivial
#: memory-exhaustion vector. Cap before reading.
MAX_WEBHOOK_BYTES = 256 * 1024


async def _apply_outcome(conn, result: str, event, event_id: str, source: str):
    """Attribute an accepted event to a recovery, if it belongs to one.

    Runs for both the real and replay paths so they cannot diverge. The
    Outcome Engine itself decides whether the event may COUNT as recovered
    revenue — a replay is linked but never counted.
    """
    if result != ACCEPTED or event is None:
        return None
    return await record_outcome(
        conn,
        event_id=event_id,
        event_type=event.event_type,
        reference_id=event.reference_id,
        payment_link_id=event.payment_link_id,
        amount_paise=event.amount_paise,
        source=source,
    )


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
    # Reject oversized bodies on the declared length before buffering.
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_WEBHOOK_BYTES:
        response.status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
        return {"status": "rejected", "reason": "payload_too_large"}

    raw_body = await request.body()
    if len(raw_body) > MAX_WEBHOOK_BYTES:
        # Covers chunked transfers, which carry no content-length.
        response.status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
        return {"status": "rejected", "reason": "payload_too_large"}

    settings = get_settings()

    outcome = None
    async with db.pool().acquire() as conn:
        result, event = await ingest_webhook(
            conn,
            raw_body=raw_body,
            signature=x_razorpay_signature,
            event_id=x_razorpay_event_id,
            secret=settings.razorpay_webhook_secret,
        )
        outcome = await _apply_outcome(
            conn, result, event, x_razorpay_event_id or "", "razorpay")

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
        "recovery": None if outcome is None else {
            "matched": outcome.matched,
            "execution_id": outcome.execution_id,
            "recovered_paise": outcome.recovered_paise,
            "counted": outcome.counted,
            "reason": outcome.reason,
        },
    }


@router.post(
    "/dev/replay-webhook",
    dependencies=[Depends(require_local_dev), Depends(require_api_key)],
)
async def replay_webhook(request: Request, response: Response):
    """Dev-only: replay a webhook payload through the REAL handler.

    This does NOT bypass signature verification for a genuine delivery: it
    signs the payload with the configured webhook secret and the result is
    verified exactly like a real one (decision D3). There is one ingest path.

    But because it self-signs, reaching this endpoint IS equivalent to knowing
    the webhook secret. It is therefore loopback-only AND API-key protected,
    and events it creates are tagged source='replay' so the Outcome Engine can
    exclude them from any figure presented as real recovered revenue.
    """
    settings = get_settings()
    body = await request.json()
    payload = body.get("payload")
    event_id = body.get("event_id")
    if not payload or not event_id:
        response.status_code = status.HTTP_400_BAD_REQUEST
        return {"status": "rejected", "reason": "payload and event_id required"}

    raw = json.dumps(payload).encode()
    signature = compute_signature(raw, settings.razorpay_webhook_secret)

    outcome = None
    async with db.pool().acquire() as conn:
        result, event = await ingest_webhook(
            conn,
            raw_body=raw,
            signature=signature,
            event_id=event_id,
            secret=settings.razorpay_webhook_secret,
            source="replay",
        )
        outcome = await _apply_outcome(conn, result, event, event_id, "replay")

    return {
        "status": "ok",
        "result": result,
        "event_type": event.event_type if event else None,
        "deduplicated": result == DUPLICATE,
        "signature_verified": True,
        "recovery": None if outcome is None else {
            "matched": outcome.matched,
            "execution_id": outcome.execution_id,
            # Always 0 for a replay: it cannot prove a customer paid.
            "recovered_paise": outcome.recovered_paise,
            "counted": outcome.counted,
            "reason": outcome.reason,
        },
    }
