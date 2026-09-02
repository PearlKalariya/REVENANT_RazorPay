"""Webhook ingest.

Receive → verify signature → deduplicate → persist → normalize.

Ordering is a security property, not a style choice:

1. **Verify before anything else.** An unverified payload is attacker-controlled
   data. It is never parsed for meaning, never used to look up a payment, and
   never written as an event. We record only that a rejection happened.
2. **Deduplicate before processing.** Razorpay retries. The same event WILL
   arrive more than once. Uniqueness is enforced by a database constraint, so
   the guarantee holds even if this code is wrong.
3. **Persist the raw payload.** The stored event is the evidence an outcome
   actually happened. Metrics that cannot point at a verified event do not
   count.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import asyncpg

from .webhook import verify_signature

log = logging.getLogger(__name__)

# Only these events are acted upon. Anything else is stored and ignored —
# an unexpected event type must never fall through into recovery logic.
HANDLED_EVENTS = frozenset(
    {
        "payment_link.paid",
        "payment.captured",
        "payment.failed",
        "payment_link.expired",
    }
)


class IngestResult(str):
    pass


ACCEPTED = "accepted"
DUPLICATE = "duplicate"
INVALID_SIGNATURE = "invalid_signature"
MALFORMED = "malformed"
IGNORED = "ignored_event_type"


@dataclass(frozen=True)
class NormalizedEvent:
    """Razorpay's payload shape flattened to what REVENANT cares about."""

    event_id: str
    event_type: str
    payment_id: str | None
    payment_link_id: str | None
    reference_id: str | None
    amount_minor: int | None
    status: str | None


def normalize(payload: dict) -> NormalizedEvent | None:
    """Flatten a Razorpay webhook payload.

    Returns None if the payload does not have the shape we expect. Callers
    treat that as MALFORMED — we do not guess at missing fields, because
    guessing about a financial event is how money gets misattributed.
    """
    event_type = payload.get("event")
    if not event_type:
        return None

    entities = payload.get("payload") or {}
    payment = (entities.get("payment") or {}).get("entity") or {}
    link = (entities.get("payment_link") or {}).get("entity") or {}

    if not payment and not link:
        return None

    amount = payment.get("amount") if payment else link.get("amount")
    if amount is not None and not isinstance(amount, int):
        return None

    return NormalizedEvent(
        # Razorpay sends the event id in the X-Razorpay-Event-Id header; the
        # caller injects it into the payload dict under this key.
        event_id=payload.get("_event_id") or "",
        event_type=event_type,
        payment_id=payment.get("id"),
        payment_link_id=link.get("id"),
        reference_id=link.get("reference_id") or payment.get("reference_id"),
        amount_minor=amount,
        status=payment.get("status") or link.get("status"),
    )


async def ingest_webhook(
    conn: asyncpg.Connection,
    *,
    raw_body: bytes,
    signature: str | None,
    event_id: str | None,
    secret: str,
    source: str = "razorpay",
) -> tuple[str, NormalizedEvent | None]:
    """Ingest one webhook. Returns (result, normalized_event | None).

    Never raises on bad input. Every rejection is a return value, so a caller
    cannot swallow a security failure in a try/except and carry on.
    """
    # --- 1. Signature. Before anything touches the payload. ----------------
    if not verify_signature(raw_body, signature, secret):
        # Log enough to diagnose WHICH delivery failed without ever logging the
        # payload (may contain customer PII) or the signature itself.
        log.warning(
            "webhook.rejected reason=invalid_signature bytes=%d event_id=%s "
            "sig_header=%s secret_configured=%s",
            len(raw_body),
            event_id or "<none>",
            "present" if signature else "MISSING",
            bool(secret),
        )
        return INVALID_SIGNATURE, None

    # --- 2. Parse. Only now is the payload trustworthy enough to read. -----
    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return MALFORMED, None
    if not isinstance(payload, dict):
        return MALFORMED, None

    # Razorpay's own event id is the dedupe key. Without it we cannot promise
    # exactly-once, so we refuse rather than risk double-processing.
    if not event_id:
        return MALFORMED, None
    payload["_event_id"] = event_id

    event = normalize(payload)
    if event is None:
        return MALFORMED, None

    # --- 3. Persist + dedupe. The UNIQUE constraint is the real guarantee. --
    inserted = await conn.fetchval(
        """
        INSERT INTO payment_events
            (event_id, payment_id, event_type, payload, signature_valid, source)
        VALUES ($1, $2, $3, $4, TRUE, $5)
        ON CONFLICT (event_id) DO NOTHING
        RETURNING id
        """,
        event_id,
        # Only link to a payment we already know about; a webhook must not be
        # able to create payment rows.
        await _known_payment_id(conn, event.payment_id),
        event.event_type,
        json.dumps(payload),
        source,
    )

    if inserted is None:
        log.info("webhook.duplicate event_id=%s", event_id)
        return DUPLICATE, event

    if event.event_type not in HANDLED_EVENTS:
        return IGNORED, event

    return ACCEPTED, event


async def _known_payment_id(conn: asyncpg.Connection, payment_id: str | None):
    if not payment_id:
        return None
    exists = await conn.fetchval("SELECT 1 FROM payments WHERE id = $1", payment_id)
    return payment_id if exists else None
