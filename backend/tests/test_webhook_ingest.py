"""Webhook ingest integration tests.

These run against the local Postgres from docker-compose, because the
deduplication guarantee lives in a DATABASE constraint. Mocking the database
would test nothing that matters here.

Skipped automatically if Postgres is not reachable, so the unit suite still
runs anywhere.
"""

from __future__ import annotations

import json
import uuid

import asyncpg
import pytest

from backend.config import get_settings
from backend.integrations.ingest import (
    ACCEPTED,
    DUPLICATE,
    IGNORED,
    INVALID_SIGNATURE,
    MALFORMED,
    ingest_webhook,
    normalize,
)
from backend.integrations.webhook import compute_signature

SECRET = "test_whsec_for_ingest_tests_only"


def make_payload(amount=240000, event="payment_link.paid", payment_id="pay_SYN00001"):
    return {
        "event": event,
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_T1",
                    "amount": amount,
                    "status": "paid",
                    "reference_id": "ref_1",
                }
            },
            "payment": {
                "entity": {"id": payment_id, "amount": amount, "status": "captured"}
            },
        },
    }


def signed(payload: dict) -> tuple[bytes, str]:
    raw = json.dumps(payload).encode()
    return raw, compute_signature(raw, SECRET)


def new_event_id() -> str:
    return f"evt_test_{uuid.uuid4().hex[:12]}"


@pytest.fixture
async def conn():
    try:
        c = await asyncpg.connect(get_settings().database_url, timeout=3)
    except Exception:
        pytest.skip("Postgres not reachable — run `docker compose up -d db`")
    tx = c.transaction()
    await tx.start()
    try:
        yield c
    finally:
        # Roll back so tests never leave rows behind.
        await tx.rollback()
        await c.close()


async def _ingest(conn, raw, sig, event_id, secret=SECRET, source="razorpay"):
    return await ingest_webhook(
        conn, raw_body=raw, signature=sig, event_id=event_id,
        secret=secret, source=source,
    )


# --- signature ------------------------------------------------------------


async def test_valid_signature_accepted(conn):
    raw, sig = signed(make_payload())
    result, event = await _ingest(conn, raw, sig, new_event_id())
    assert result == ACCEPTED
    assert event.event_type == "payment_link.paid"
    assert event.amount_minor == 240000


async def test_forged_signature_rejected_and_not_persisted(conn):
    raw, _ = signed(make_payload())
    eid = new_event_id()
    result, _ = await _ingest(conn, raw, "de" * 32, eid)
    assert result == INVALID_SIGNATURE
    assert await conn.fetchval(
        "SELECT count(*) FROM payment_events WHERE event_id=$1", eid
    ) == 0


async def test_tampered_amount_rejected(conn):
    """The attack: intercept a real webhook, inflate the amount, replay it."""
    payload = make_payload(amount=240000)
    raw, sig = signed(payload)
    tampered = raw.replace(b"240000", b"99900000")
    result, _ = await _ingest(conn, tampered, sig, new_event_id())
    assert result == INVALID_SIGNATURE


async def test_missing_signature_rejected(conn):
    raw, _ = signed(make_payload())
    result, _ = await _ingest(conn, raw, None, new_event_id())
    assert result == INVALID_SIGNATURE


# --- deduplication (mandated failure scenario 4) --------------------------


async def test_duplicate_event_processed_once(conn):
    raw, sig = signed(make_payload())
    eid = new_event_id()

    first, _ = await _ingest(conn, raw, sig, eid)
    second, _ = await _ingest(conn, raw, sig, eid)
    third, _ = await _ingest(conn, raw, sig, eid)

    assert first == ACCEPTED
    assert second == DUPLICATE
    assert third == DUPLICATE
    assert await conn.fetchval(
        "SELECT count(*) FROM payment_events WHERE event_id=$1", eid
    ) == 1


async def test_duplicate_is_actionable_only_once(conn):
    """Only the first delivery may trigger downstream recovery work."""
    raw, sig = signed(make_payload())
    eid = new_event_id()
    results = [(await _ingest(conn, raw, sig, eid))[0] for _ in range(5)]
    assert results.count(ACCEPTED) == 1


# --- malformed input ------------------------------------------------------


async def test_malformed_json_rejected(conn):
    bad = b"{not json"
    result, _ = await _ingest(conn, bad, compute_signature(bad, SECRET), new_event_id())
    assert result == MALFORMED


async def test_missing_event_id_rejected(conn):
    """Without Razorpay's event id we cannot promise exactly-once, so we
    refuse rather than risk double-processing."""
    raw, sig = signed(make_payload())
    result, _ = await _ingest(conn, raw, sig, None)
    assert result == MALFORMED


async def test_payload_without_entities_rejected(conn):
    payload = {"event": "payment_link.paid", "payload": {}}
    raw, sig = signed(payload)
    result, _ = await _ingest(conn, raw, sig, new_event_id())
    assert result == MALFORMED


async def test_non_integer_amount_rejected(conn):
    payload = make_payload()
    payload["payload"]["payment"]["entity"]["amount"] = "240000"
    raw, sig = signed(payload)
    result, _ = await _ingest(conn, raw, sig, new_event_id())
    assert result == MALFORMED


# --- event type filtering -------------------------------------------------


async def test_unhandled_event_stored_but_not_actionable(conn):
    payload = {
        "event": "payout.processed",
        "payload": {"payment": {"entity": {"id": "x", "amount": 100}}},
    }
    raw, sig = signed(payload)
    result, _ = await _ingest(conn, raw, sig, new_event_id())
    assert result == IGNORED


# --- provenance -----------------------------------------------------------


async def test_replay_source_recorded_but_verification_identical(conn):
    """Replayed events are tagged, but pass the same signature check (D3)."""
    raw, sig = signed(make_payload())
    eid = new_event_id()
    result, _ = await _ingest(conn, raw, sig, eid, source="replay")
    assert result == ACCEPTED
    assert await conn.fetchval(
        "SELECT source FROM payment_events WHERE event_id=$1", eid
    ) == "replay"


async def test_unknown_payment_id_not_linked(conn):
    """A webhook must not be able to invent a payment row."""
    raw, sig = signed(make_payload(payment_id="pay_DOES_NOT_EXIST"))
    eid = new_event_id()
    await _ingest(conn, raw, sig, eid)
    assert await conn.fetchval(
        "SELECT payment_id FROM payment_events WHERE event_id=$1", eid
    ) is None


# --- normalize (pure) -----------------------------------------------------


def test_normalize_extracts_fields():
    payload = make_payload()
    payload["_event_id"] = "evt_1"
    event = normalize(payload)
    assert event.event_id == "evt_1"
    assert event.payment_id == "pay_SYN00001"
    assert event.payment_link_id == "plink_T1"
    assert event.amount_minor == 240000


def test_normalize_returns_none_for_missing_event():
    assert normalize({"payload": {}}) is None
