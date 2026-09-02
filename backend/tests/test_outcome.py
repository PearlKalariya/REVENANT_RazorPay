"""Outcome Engine tests.

The recovered-revenue figure is the headline claim of this whole system, so
these tests are mostly about what must NOT count toward it.
"""

from __future__ import annotations

import asyncpg
import pytest

from backend.config import get_settings
from backend.recovery.outcome import record_outcome

KEY = "rv_testkey000000000000000000000000"


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
        await tx.rollback()
        await c.close()


async def _execution(conn, *, amount=240_000, ref="plink_T1"):
    await conn.execute("INSERT INTO merchants(id,name) VALUES('m_o','O')")
    await conn.execute(
        "INSERT INTO customers(id,merchant_id) VALUES('c_o','m_o')")
    await conn.execute(
        "INSERT INTO payments(id,merchant_id,customer_id,amount_minor,status,"
        "method,created_at,is_synthetic)"
        " VALUES('pay_o','m_o','c_o',$1,'failed','upi',now(),TRUE)", amount)
    inc = await conn.fetchval(
        "INSERT INTO revenue_incidents(merchant_id,title) VALUES('m_o','t')"
        " RETURNING id")
    aid = await conn.fetchval(
        "INSERT INTO recovery_actions(incident_id,payment_id,customer_id,action,"
        "amount_minor,status)"
        " VALUES($1,'pay_o','c_o','CREATE_PAYMENT_LINK',$2,'executed')"
        " RETURNING id", inc, amount)
    return await conn.fetchval(
        "INSERT INTO execution_records(action_id,idempotency_key,status,"
        "amount_minor,razorpay_ref)"
        " VALUES($1,$2,'succeeded',$3,$4) RETURNING id", aid, KEY, amount, ref)


async def _record(conn, **kw):
    """Record an outcome for an event that has already been ingested.

    The event row is inserted first because recovery_outcomes.verified_by_event
    is a FOREIGN KEY into payment_events — an outcome must name a real stored
    event as its evidence, and cannot be conjured without one. The live flow
    satisfies this naturally: ingest persists the event, then the Outcome
    Engine runs.
    """
    base = dict(event_id="evt_1", event_type="payment_link.paid",
                reference_id=KEY, payment_link_id=None,
                amount_minor=240_000, source="razorpay")
    base.update(kw)
    await conn.execute(
        "INSERT INTO payment_events(event_id,event_type,payload,"
        "signature_valid,source) VALUES($1,$2,'{}',TRUE,$3)"
        " ON CONFLICT (event_id) DO NOTHING",
        base["event_id"], base["event_type"], base["source"])
    return await record_outcome(conn, **base)


# --- the property that makes the number trustworthy -----------------------


async def test_replayed_event_never_counts_as_revenue(conn):
    """A replay is signed with OUR secret, so it proves nothing about whether a
    customer paid. It is linked for traceability, never counted.

    Without this, anyone reaching the replay endpoint could fabricate revenue —
    which is exactly the P0 previously found in this system.
    """
    exec_id = await _execution(conn)
    result = await _record(conn, source="replay")
    assert result.matched is True
    assert result.counted is False
    assert result.recovered_minor == 0
    # Scoped to THIS execution: a global count would pick up real rows written
    # outside this test's transaction and fail for the wrong reason.
    assert await conn.fetchval(
        "SELECT count(*) FROM recovery_outcomes WHERE execution_id=$1",
        exec_id) == 0


async def test_razorpay_event_counts(conn):
    exec_id = await _execution(conn)
    result = await _record(conn)
    assert result.counted is True
    assert result.recovered_minor == 240_000
    row = await conn.fetchrow(
        "SELECT recovered_minor, succeeded, verified_by_event"
        " FROM recovery_outcomes WHERE execution_id=$1", exec_id)
    assert row["recovered_minor"] == 240_000
    assert row["succeeded"] is True
    assert row["verified_by_event"] is not None, "outcome must name its evidence"


async def test_amount_comes_from_razorpay_not_from_us(conn):
    """We record what was actually paid, not what we hoped."""
    await _execution(conn, amount=240_000)
    result = await _record(conn, amount_minor=100_000)
    assert result.recovered_minor == 100_000


# --- duplicates -----------------------------------------------------------


async def test_duplicate_webhook_does_not_double_count(conn):
    exec_id = await _execution(conn)
    first = await _record(conn, event_id="evt_a")
    second = await _record(conn, event_id="evt_b")
    assert first.counted is True
    assert second.counted is False
    # Scoped to THIS execution. Asserting a global total made the test depend
    # on whatever else the database happened to contain.
    total = await conn.fetchval(
        "SELECT coalesce(sum(recovered_minor),0) FROM recovery_outcomes"
        " WHERE execution_id=$1", exec_id)
    assert total == 240_000
    assert await conn.fetchval(
        "SELECT count(*) FROM recovery_outcomes WHERE execution_id=$1",
        exec_id) == 1


# --- matching -------------------------------------------------------------


async def test_unmatched_event_is_ignored(conn):
    await _execution(conn)
    result = await _record(conn, reference_id="rv_somethingelse")
    assert result.matched is False
    assert result.recovered_minor == 0


async def test_matches_by_payment_link_id_when_no_reference(conn):
    await _execution(conn, ref="plink_XYZ")
    result = await _record(conn, reference_id=None, payment_link_id="plink_XYZ")
    assert result.matched is True and result.counted is True


async def test_no_fuzzy_matching_on_amount(conn):
    """Attributing revenue by amount alone would credit the wrong recovery."""
    await _execution(conn, amount=240_000, ref="plink_A")
    result = await _record(conn, reference_id=None, payment_link_id=None)
    assert result.matched is False


# --- failure events -------------------------------------------------------


async def test_expired_link_records_zero_recovered(conn):
    exec_id = await _execution(conn)
    result = await _record(conn, event_type="payment_link.expired")
    assert result.counted is True
    assert result.succeeded is False
    assert result.recovered_minor == 0
    assert await conn.fetchval(
        "SELECT recovered_minor FROM recovery_outcomes WHERE execution_id=$1",
        exec_id) == 0


async def test_failed_payment_records_zero(conn):
    await _execution(conn)
    result = await _record(conn, event_type="payment.failed")
    assert result.succeeded is False and result.recovered_minor == 0


async def test_irrelevant_event_type_ignored(conn):
    await _execution(conn)
    result = await _record(conn, event_type="payout.processed")
    assert result.matched is False


# --- side effects ---------------------------------------------------------


async def test_success_marks_payment_captured(conn):
    await _execution(conn)
    await _record(conn)
    assert await conn.fetchval(
        "SELECT status::text FROM payments WHERE id='pay_o'") == "captured"


async def test_failure_leaves_payment_failed(conn):
    await _execution(conn)
    await _record(conn, event_type="payment_link.expired")
    assert await conn.fetchval(
        "SELECT status::text FROM payments WHERE id='pay_o'") == "failed"


async def test_outcome_writes_audit_event(conn):
    exec_id = await _execution(conn)
    await _record(conn)
    row = await conn.fetchrow(
        "SELECT actor,event_type,amount_minor FROM audit_events"
        " WHERE execution_id=$1 ORDER BY id DESC LIMIT 1", exec_id)
    assert row["actor"] == "OUTCOME_ENGINE"
    assert row["event_type"] == "REVENUE_RECOVERED"
    assert row["amount_minor"] == 240_000


async def test_untrusted_source_is_audited(conn):
    exec_id = await _execution(conn)
    await _record(conn, source="replay")
    row = await conn.fetchrow(
        "SELECT event_type FROM audit_events WHERE execution_id=$1"
        " ORDER BY id DESC LIMIT 1", exec_id)
    assert row["event_type"] == "OUTCOME_IGNORED_UNTRUSTED_SOURCE"
