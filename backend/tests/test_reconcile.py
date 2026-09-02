"""Outcome reconciliation tests.

Webhooks must never be assumed to arrive. This path exists because five real
payments were confirmed by Razorpay while ZERO webhooks reached the system.
"""

from __future__ import annotations

import asyncpg
import pytest

from backend.config import get_settings
from backend.integrations.razorpay_client import RazorpayError
from backend.recovery.reconcile import reconcile_outcomes

KEY = "rv_reconcile00000000000000000000"


class FakeClient:
    def __init__(self, links: dict, fail: bool = False):
        self.links = links
        self.fail = fail
        self.fetches: list[str] = []

    async def fetch_payment_link(self, link_id: str) -> dict:
        self.fetches.append(link_id)
        if self.fail:
            raise RazorpayError("boom", retryable=True)
        return self.links[link_id]


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


async def _execution(conn, *, amount=204_900, ref="plink_R1"):
    await conn.execute("INSERT INTO merchants(id,name) VALUES('m_r','R')")
    await conn.execute("INSERT INTO customers(id,merchant_id) VALUES('c_r','m_r')")
    await conn.execute(
        "INSERT INTO payments(id,merchant_id,customer_id,amount_minor,status,"
        "method,created_at,is_synthetic)"
        " VALUES('pay_r','m_r','c_r',$1,'failed','upi',now(),TRUE)", amount)
    inc = await conn.fetchval(
        "INSERT INTO revenue_incidents(merchant_id,title) VALUES('m_r','t') RETURNING id")
    aid = await conn.fetchval(
        "INSERT INTO recovery_actions(incident_id,payment_id,customer_id,action,"
        "amount_minor,status) VALUES($1,'pay_r','c_r','CREATE_PAYMENT_LINK',$2,"
        "'executed') RETURNING id", inc, amount)
    return await conn.fetchval(
        "INSERT INTO execution_records(action_id,idempotency_key,status,"
        "amount_minor,razorpay_ref) VALUES($1,$2,'succeeded',$3,$4) RETURNING id",
        aid, KEY, amount, ref)


async def test_paid_link_becomes_recovered_revenue(conn):
    exec_id = await _execution(conn)
    client = FakeClient({"plink_R1": {"status": "paid", "amount_paid": 204_900}})
    r = await reconcile_outcomes(conn, client, merchant_id="m_r")
    assert r.newly_paid == 1
    assert r.recovered_minor == 204_900
    assert await conn.fetchval(
        "SELECT recovered_minor FROM recovery_outcomes WHERE execution_id=$1",
        exec_id) == 204_900


async def test_amount_comes_from_razorpay_not_from_us(conn):
    """We record what was actually paid. A partial payment is not a full one."""
    await _execution(conn, amount=204_900)
    client = FakeClient({"plink_R1": {"status": "paid", "amount_paid": 100_000}})
    r = await reconcile_outcomes(conn, client, merchant_id="m_r")
    assert r.recovered_minor == 100_000


async def test_unpaid_link_recovers_nothing(conn):
    exec_id = await _execution(conn)
    client = FakeClient({"plink_R1": {"status": "created", "amount": 204_900}})
    r = await reconcile_outcomes(conn, client, merchant_id="m_r")
    assert r.newly_paid == 0
    assert r.recovered_minor == 0
    # Scoped to THIS execution. A global count picks up real reconciled
    # outcomes committed outside this test's transaction.
    assert await conn.fetchval(
        "SELECT count(*) FROM recovery_outcomes WHERE execution_id=$1",
        exec_id) == 0


async def test_expired_link_records_a_zero_outcome(conn):
    """An expired link is a settled result: the recovery did not convert."""
    exec_id = await _execution(conn)
    client = FakeClient({"plink_R1": {"status": "expired", "amount": 204_900}})
    r = await reconcile_outcomes(conn, client, merchant_id="m_r")
    assert r.newly_closed == 1
    row = await conn.fetchrow(
        "SELECT recovered_minor, succeeded FROM recovery_outcomes"
        " WHERE execution_id=$1", exec_id)
    assert row["recovered_minor"] == 0
    assert row["succeeded"] is False


async def test_already_reconciled_is_not_rechecked(conn):
    await _execution(conn)
    client = FakeClient({"plink_R1": {"status": "paid", "amount_paid": 204_900}})
    await reconcile_outcomes(conn, client, merchant_id="m_r")
    second = await reconcile_outcomes(conn, client, merchant_id="m_r")
    assert second.checked == 0, "an execution with an outcome must not be re-fetched"


async def test_reconciliation_does_not_double_count(conn):
    exec_id = await _execution(conn)
    client = FakeClient({"plink_R1": {"status": "paid", "amount_paid": 204_900}})
    await reconcile_outcomes(conn, client, merchant_id="m_r")
    await reconcile_outcomes(conn, client, merchant_id="m_r")
    total = await conn.fetchval(
        "SELECT coalesce(sum(recovered_minor),0) FROM recovery_outcomes"
        " WHERE execution_id=$1", exec_id)
    assert total == 204_900
    assert await conn.fetchval(
        "SELECT count(*) FROM recovery_outcomes WHERE execution_id=$1",
        exec_id) == 1


async def test_fetch_failure_is_counted_not_swallowed(conn):
    await _execution(conn)
    r = await reconcile_outcomes(conn, FakeClient({}, fail=True), merchant_id="m_r")
    assert r.errors == 1
    assert r.newly_paid == 0


async def test_outcome_names_its_evidence(conn):
    """A pulled answer is still evidence, and must be recorded as such — so an
    auditor can tell a push-confirmed recovery from a pull-confirmed one."""
    exec_id = await _execution(conn)
    client = FakeClient({"plink_R1": {"status": "paid", "amount_paid": 204_900}})
    await reconcile_outcomes(conn, client, merchant_id="m_r")
    event_id = await conn.fetchval(
        "SELECT verified_by_event FROM recovery_outcomes WHERE execution_id=$1",
        exec_id)
    assert event_id is not None
    source = await conn.fetchval(
        "SELECT source FROM payment_events WHERE event_id=$1", event_id)
    assert source == "razorpay_api"


async def test_replay_source_still_cannot_count(conn):
    """Widening trust to Razorpay's API must NOT widen it to replays."""
    from backend.recovery.outcome import TRUSTED_SOURCES
    assert "replay" not in TRUSTED_SOURCES
    assert TRUSTED_SOURCES == {"razorpay", "razorpay_api"}
