"""Action Executor tests.

The executor is the only component that can move money, so every refusal path
gets its own test. A guard without a test is a guard that will be removed by
someone who does not know why it is there.

Razorpay is stubbed: these assert the executor's DECISIONS, not Razorpay's
behaviour. The real integration is verified separately against test mode.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from backend.config import get_settings
from backend.integrations.razorpay_client import PaymentLink, RazorpayError
from backend.recovery.executor import (
    RAZORPAY_REFERENCE_ID_MAX,
    ExecutionRefused,
    execute_action,
    idempotency_key,
)

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


class FakeRazorpay:
    """Records calls so tests can assert money did or did not move."""

    def __init__(self, *, fail: RazorpayError | None = None):
        self.calls: list[dict] = []
        self.fail = fail

    async def create_payment_link(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise self.fail
        return PaymentLink(id="plink_FAKE", short_url="https://rzp.io/x",
                           status="created", amount_paise=kw["amount_paise"],
                           reference_id=kw["reference_id"])


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


async def _fixture(conn, *, policy="AUTO_APPROVED", status="approved",
                   payment_status="failed", opted_out=False, amount=25_000,
                   expires_in_min=60, approval=None, ruled_amount=None):
    """Build one isolated action with its policy decision."""
    await conn.execute("INSERT INTO merchants(id,name) VALUES('m_x','X')")
    await conn.execute(
        "INSERT INTO customers(id,merchant_id,email,phone,opted_out)"
        " VALUES('c_x','m_x','a@b.test','+919800000000',$1)", opted_out)
    await conn.execute(
        "INSERT INTO payments(id,merchant_id,customer_id,amount_paise,status,"
        "method,created_at,is_synthetic)"
        " VALUES('pay_x','m_x','c_x',$1,$2::payment_status,'upi',$3,TRUE)",
        amount, payment_status, NOW - timedelta(hours=2))
    inc = await conn.fetchval(
        "INSERT INTO revenue_incidents(merchant_id,title) VALUES('m_x','t')"
        " RETURNING id")
    aid = await conn.fetchval(
        "INSERT INTO recovery_actions(incident_id,payment_id,customer_id,action,"
        "amount_paise,status,proposed_at,expires_at)"
        " VALUES($1,'pay_x','c_x','CREATE_PAYMENT_LINK',$2,$3::action_status,$4,$5)"
        " RETURNING id",
        inc, amount, status, NOW,
        NOW + timedelta(minutes=expires_in_min))
    await conn.execute(
        "INSERT INTO policy_decisions(action_id,phase,result,rule,reason,"
        "policy_version,policy_hash,metadata,evaluated_at)"
        " VALUES($1,'authorization',$2::policy_result,'r','because','v1',"
        "'hash_authorization_v1',$3,$4)",
        aid, policy,
        json.dumps({"amount_paise": ruled_amount if ruled_amount is not None else amount}),
        NOW)
    if approval is not None:
        await conn.execute(
            "INSERT INTO approvals(action_id,approved,approver) VALUES($1,$2,'h')",
            aid, approval)
    return aid


async def _run(conn, aid, client=None, now=NOW):
    client = client or FakeRazorpay()
    return await execute_action(conn, client, action_id=aid,
                                merchant_id="m_x", settings=get_settings(),
                                now=now), client


# --- refusals -------------------------------------------------------------


async def test_unknown_action_refused(conn):
    with pytest.raises(ExecutionRefused) as e:
        await _run(conn, 999_999)
    assert e.value.rule == "action_not_found"


async def test_wrong_merchant_refused(conn):
    aid = await _fixture(conn)
    with pytest.raises(ExecutionRefused) as e:
        await execute_action(conn, FakeRazorpay(), action_id=aid,
                             merchant_id="m_other", settings=get_settings(), now=NOW)
    assert e.value.rule == "wrong_merchant"


async def test_no_policy_decision_refused(conn):
    """An action with no recorded ruling is unexplainable, so it cannot run."""
    aid = await _fixture(conn)
    await conn.execute("DELETE FROM policy_decisions WHERE action_id=$1", aid)
    with pytest.raises(ExecutionRefused) as e:
        await _run(conn, aid)
    assert e.value.rule == "no_policy_decision"


async def test_blocked_policy_refused(conn):
    aid = await _fixture(conn, policy="BLOCKED", status="denied")
    with pytest.raises(ExecutionRefused) as e:
        await _run(conn, aid)
    assert e.value.rule == "policy_blocked"


async def test_requires_approval_without_approval_refused(conn):
    aid = await _fixture(conn, policy="REQUIRES_APPROVAL", status="awaiting_approval")
    with pytest.raises(ExecutionRefused) as e:
        await _run(conn, aid)
    assert e.value.rule == "approval_missing"


async def test_denied_approval_refused(conn):
    aid = await _fixture(conn, policy="REQUIRES_APPROVAL",
                         status="awaiting_approval", approval=False)
    with pytest.raises(ExecutionRefused) as e:
        await _run(conn, aid)
    assert e.value.rule == "approval_denied"


async def test_expired_action_refused(conn):
    aid = await _fixture(conn)
    with pytest.raises(ExecutionRefused) as e:
        await _run(conn, aid, now=NOW + timedelta(hours=3))
    assert e.value.rule == "action_expired"


async def test_amount_changed_after_ruling_refused(conn):
    """Post-approval tamper check: a changed amount invalidates the ruling."""
    aid = await _fixture(conn, amount=25_000, ruled_amount=5_000)
    with pytest.raises(ExecutionRefused) as e:
        await _run(conn, aid)
    assert e.value.rule == "amount_changed"


async def test_already_paid_refused(conn):
    """The race the Failure Lab demonstrates: customer paid by other means
    between planning and execution.

    The rule name comes from the Policy Engine itself. The executor no longer
    keeps its own parallel copy of this check — one source of truth for what is
    permitted, evaluated fresh at execution time.
    """
    aid = await _fixture(conn, payment_status="captured")
    with pytest.raises(ExecutionRefused) as e:
        await _run(conn, aid)
    assert e.value.rule == "already_paid"


async def test_opted_out_customer_refused(conn):
    aid = await _fixture(conn, opted_out=True)
    with pytest.raises(ExecutionRefused) as e:
        await _run(conn, aid)
    assert e.value.rule == "customer_opted_out"


async def test_no_razorpay_call_on_any_refusal(conn):
    """The point of every refusal: no money moved."""
    client = FakeRazorpay()
    for kwargs in ({"policy": "BLOCKED", "status": "denied"},
                   {"opted_out": True},
                   {"payment_status": "captured"}):
        await conn.execute("ROLLBACK TO SAVEPOINT sp" if False else "SAVEPOINT sp")
        aid = await _fixture(conn, **kwargs)
        with pytest.raises(ExecutionRefused):
            await execute_action(conn, client, action_id=aid, merchant_id="m_x",
                                 settings=get_settings(), now=NOW)
        await conn.execute("ROLLBACK TO SAVEPOINT sp")
    assert client.calls == []


# --- success and idempotency ----------------------------------------------


async def test_successful_execution(conn):
    aid = await _fixture(conn)
    result, client = await _run(conn, aid)
    assert result.status == "succeeded"
    assert result.razorpay_ref == "plink_FAKE"
    assert len(client.calls) == 1


async def test_approved_with_human_approval_executes(conn):
    aid = await _fixture(conn, policy="REQUIRES_APPROVAL", status="approved",
                         approval=True)
    result, client = await _run(conn, aid)
    assert result.status == "succeeded"
    assert len(client.calls) == 1


async def test_second_run_reuses_and_does_not_recharge(conn):
    aid = await _fixture(conn)
    client = FakeRazorpay()
    first, _ = await _run(conn, aid, client)
    second, _ = await _run(conn, aid, client)
    assert second.reused is True
    assert second.execution_id == first.execution_id
    assert len(client.calls) == 1, "Razorpay called twice — duplicate charge"


async def test_timeout_leaves_pending_not_failed(conn):
    """A timeout may have succeeded on Razorpay's side. Marking it failed and
    retrying is how a timeout becomes a double charge."""
    aid = await _fixture(conn)
    client = FakeRazorpay(fail=RazorpayError("timed out", retryable=True))
    result, _ = await _run(conn, aid, client)
    assert result.status == "pending"


async def test_retry_after_timeout_returns_existing_execution(conn):
    """Regression: this previously refused as 'not_executable', because the
    state check ran before the idempotency check."""
    aid = await _fixture(conn)
    timing_out = FakeRazorpay(fail=RazorpayError("timed out", retryable=True))
    first, _ = await _run(conn, aid, timing_out)
    retry, _ = await _run(conn, aid, FakeRazorpay())
    assert retry.reused is True
    assert retry.execution_id == first.execution_id
    assert retry.status == "pending"


async def test_hard_error_marks_failed_not_pending(conn):
    aid = await _fixture(conn)
    client = FakeRazorpay(fail=RazorpayError("bad request", retryable=False))
    result, _ = await _run(conn, aid, client)
    assert result.status == "failed"


async def test_execution_writes_audit_trail(conn):
    aid = await _fixture(conn)
    await _run(conn, aid)
    events = [r["event_type"] for r in await conn.fetch(
        "SELECT event_type FROM audit_events WHERE action_id=$1 ORDER BY id", aid)]
    assert "EXECUTION_STARTED" in events
    assert "PAYMENT_LINK_CREATED" in events


# --- idempotency key ------------------------------------------------------


def test_key_fits_razorpay_reference_id_limit():
    """Razorpay rejects reference_id over 40 chars. The key IS the
    reference_id, so an oversized key made every payment link 400."""
    key = idempotency_key(123456789, 987_654_321, "v1")
    assert len(key) <= RAZORPAY_REFERENCE_ID_MAX


def test_key_is_stable_and_amount_sensitive():
    a = idempotency_key(1, 25_000, "v1")
    assert a == idempotency_key(1, 25_000, "v1")
    assert a != idempotency_key(1, 25_001, "v1"), "changed amount must be a new action"
    assert a != idempotency_key(2, 25_000, "v1")
    assert a != idempotency_key(1, 25_000, "v2")


# --- runtime policy re-evaluation -----------------------------------------
#
# The stored policy decision records that an action was AUTHORISED. It does not
# prove it is still PERMITTED. Limits move between planning and execution, and
# trusting the stored ruling alone allowed a real breach: 22 actions approved
# across a day totalled ₹38,893 against a ₹25,000 cap and ALL 22 executed.


async def test_daily_cap_enforced_at_execution_time(conn):
    """A batch of individually-approved actions must not collectively breach
    the daily cap."""
    from backend.policy import PolicyConfig

    config = PolicyConfig(max_daily_recovery_paise=50_000)
    aid = await _fixture(conn, amount=30_000)
    # Another execution already committed most of today's budget.
    await conn.execute(
        "INSERT INTO execution_records(action_id,idempotency_key,status,"
        "amount_paise,created_at)"
        " VALUES($1,'rv_other000000000000000000000000','succeeded',$2,$3)",
        aid, 30_000, NOW)

    client = FakeRazorpay()
    with pytest.raises(ExecutionRefused) as e:
        await execute_action(conn, client, action_id=aid, merchant_id="m_x",
                             settings=get_settings(), now=NOW,
                             policy_config=config)
    assert e.value.rule == "daily_limit_exceeded"
    assert client.calls == [], "money moved despite breaching the cap"


async def test_pending_executions_count_against_the_cap(conn):
    """A timed-out execution may have created a link on Razorpay's side.
    Treating it as free budget would overshoot the cap by exactly the actions
    we are least sure about."""
    from backend.policy import PolicyConfig

    config = PolicyConfig(max_daily_recovery_paise=50_000)
    aid = await _fixture(conn, amount=30_000)
    await conn.execute(
        "INSERT INTO execution_records(action_id,idempotency_key,status,"
        "amount_paise,created_at)"
        " VALUES($1,'rv_pending0000000000000000000000','pending',$2,$3)",
        aid, 30_000, NOW)

    with pytest.raises(ExecutionRefused) as e:
        await execute_action(conn, FakeRazorpay(), action_id=aid,
                             merchant_id="m_x", settings=get_settings(),
                             now=NOW, policy_config=config)
    assert e.value.rule == "daily_limit_exceeded"


async def test_opt_out_after_approval_blocks_execution(conn):
    """A customer who opts out after approval must not be contacted."""
    aid = await _fixture(conn)
    await conn.execute("UPDATE customers SET opted_out=TRUE WHERE id='c_x'")
    client = FakeRazorpay()
    with pytest.raises(ExecutionRefused) as e:
        await _run(conn, aid, client)
    assert e.value.rule == "customer_opted_out"
    assert client.calls == []


async def test_runtime_block_is_audited(conn):
    """A refusal at execution time must be explainable afterwards."""
    from backend.policy import PolicyConfig

    aid = await _fixture(conn)
    await conn.execute("UPDATE customers SET opted_out=TRUE WHERE id='c_x'")
    with pytest.raises(ExecutionRefused):
        await _run(conn, aid)
    row = await conn.fetchrow(
        "SELECT actor,event_type FROM audit_events WHERE action_id=$1"
        " ORDER BY id DESC LIMIT 1", aid)
    assert row["actor"] == "POLICY_ENGINE"
    assert row["event_type"] == "EXECUTION_BLOCKED_AT_RUNTIME"


async def test_within_cap_still_executes(conn):
    """The guard must not block legitimate work."""
    from backend.policy import PolicyConfig

    aid = await _fixture(conn, amount=10_000)
    result, client = await _run(conn, aid)
    assert result.status == "succeeded"
    assert len(client.calls) == 1


# --- concurrency ----------------------------------------------------------


async def test_lost_race_returns_winners_result_not_an_error(conn):
    """Two workers can pass the idempotency check before either inserts. The
    database prevents the double charge; the loser must receive the winner's
    result, not an unhandled exception."""
    from backend.recovery.executor import idempotency_key

    aid = await _fixture(conn)
    row = await conn.fetchrow(
        "SELECT amount_paise FROM recovery_actions WHERE id=$1", aid)
    key = idempotency_key(aid, int(row["amount_paise"]), "v1")
    # A competing worker claims the key first.
    winner_id = await conn.fetchval(
        "INSERT INTO execution_records(action_id,idempotency_key,status,"
        "amount_paise) VALUES($1,$2,'succeeded',$3) RETURNING id",
        aid, key, int(row["amount_paise"]))

    result, client = await _run(conn, aid)
    assert result.reused is True
    assert result.execution_id == winner_id
    assert client.calls == [], "second Razorpay call despite an existing execution"


# --- guards that must survive optimisation --------------------------------


def test_key_length_guard_is_not_an_assert():
    """`python -O` strips asserts. A financial guard that disappears under an
    optimisation flag is not a guard."""
    import inspect
    from backend.recovery import executor

    src = inspect.getsource(executor.idempotency_key)
    assert "assert " not in src
    assert "raise ValueError" in src


async def test_daily_window_follows_the_merchants_timezone(conn):
    """A UTC day rolls over at 05:30 IST.

    Spend at 02:00 IST falls on the PREVIOUS UTC day, so a UTC-based window
    forgets it and lets the merchant's daily cap be exceeded by that amount.
    The limit is unchanged; the window it applies to must be the merchant's.
    """
    from datetime import timedelta as _td
    from datetime import timezone as _tz

    from backend.recovery.executor import daily_committed_paise

    IST = _tz(_td(hours=5, minutes=30))
    aid = await _fixture(conn, amount=1_000)

    early = datetime(2026, 8, 29, 2, 0, tzinfo=IST)     # 20:30 UTC on 28 Aug
    later = datetime(2026, 8, 29, 11, 0, tzinfo=IST)    # same IST day
    await conn.execute(
        "INSERT INTO execution_records(action_id,idempotency_key,status,"
        "amount_paise,created_at) VALUES($1,'rv_tzcheck0000000000000000000000',"
        "'succeeded',$2,$3)", aid, 5_000, early)

    in_ist = await daily_committed_paise(conn, later, "Asia/Kolkata")
    in_utc = await daily_committed_paise(conn, later, "UTC")
    assert in_ist >= 5_000, "IST window must include spend from earlier the same day"
    assert in_utc < in_ist, "UTC window drops early-IST spend — this is the bug"


def test_business_timezone_defaults_to_india():
    from backend.policy import PolicyConfig
    assert PolicyConfig().business_timezone == "Asia/Kolkata"


async def test_razorpay_rate_limit_is_retryable_not_failed(conn):
    """429 is Razorpay asking us to slow down, not a bad request.

    Classifying it with the other 4xx marked 15 executions permanently `failed`
    in a batch run — money that never moved, recorded as failed recoveries.
    """
    from backend.integrations.razorpay_client import RazorpayError

    aid = await _fixture(conn)
    client = FakeRazorpay(fail=RazorpayError("rate limited", status=429,
                                             retryable=True))
    result, _ = await _run(conn, aid, client)
    assert result.status == "pending", "a throttled call must not be marked failed"
