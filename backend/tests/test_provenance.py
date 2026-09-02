"""Policy provenance tests (D15).

Authorization history and execution authority are two separate facts. These
tests exist to stop one silently overwriting the other.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from backend.config import get_settings
from backend.integrations.razorpay_client import PaymentLink
from backend.policy import EvaluationPhase, PolicyConfig, policy_hash
from backend.recovery.executor import ExecutionRefused, execute_action
from backend.recovery.provenance import action_provenance

IST = timezone(timedelta(hours=5, minutes=30))
PLANNED = datetime(2026, 8, 28, 10, 2, 31, tzinfo=IST)
RUN_AT = datetime(2026, 8, 29, 14, 32, 11, tzinfo=IST)


class Fake:
    def __init__(self): self.calls = []
    async def create_payment_link(self, **kw):
        self.calls.append(kw)
        return PaymentLink(id="plink_x", short_url="u", status="created",
                           amount_minor=kw["amount_minor"],
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


async def _authorized_action(conn, *, amount=204_900, config=None):
    """One action authorized under `config`."""
    config = config or PolicyConfig(version="v3")
    await conn.execute("INSERT INTO merchants(id,name) VALUES('m_p','P')")
    await conn.execute("INSERT INTO customers(id,merchant_id,email) VALUES('c_p','m_p','a@b.test')")
    await conn.execute(
        "INSERT INTO payments(id,merchant_id,customer_id,amount_minor,status,"
        "method,created_at,is_synthetic)"
        " VALUES('pay_p','m_p','c_p',$1,'failed','upi',$2,TRUE)", amount, PLANNED)
    inc = await conn.fetchval(
        "INSERT INTO revenue_incidents(merchant_id,title) VALUES('m_p','t') RETURNING id")
    aid = await conn.fetchval(
        "INSERT INTO recovery_actions(incident_id,payment_id,customer_id,action,"
        "amount_minor,status,proposed_at,expires_at)"
        " VALUES($1,'pay_p','c_p','CREATE_PAYMENT_LINK',$2,'approved',$3,$4)"
        " RETURNING id",
        inc, amount, PLANNED, PLANNED + timedelta(days=7))
    await conn.execute(
        "INSERT INTO policy_decisions(action_id,phase,result,rule,reason,"
        "policy_version,policy_hash,metadata,evaluated_at)"
        " VALUES($1,'authorization','AUTO_APPROVED','within_policy','ok',$2,$3,$4,$5)",
        aid, config.version, policy_hash(config),
        json.dumps({"amount_minor": amount}), PLANNED)
    return aid


def _long_ttl(**kw):
    return PolicyConfig(action_ttl_minutes=60 * 24 * 30, **kw)


# --- the two facts stay separate ------------------------------------------


async def test_execution_decision_never_overwrites_authorization(conn):
    """The scenario: authorized under v3 at 10:02, refused under a tightened v4
    the next day. BOTH must survive in the record."""
    v3 = _long_ttl(version="v3", max_daily_recovery_minor=2_500_000)
    v4 = _long_ttl(version="v4", max_daily_recovery_minor=2_000_000)
    aid = await _authorized_action(conn, config=v3)

    # Other recovery consumed most of the day's tightened budget.
    await conn.execute(
        "INSERT INTO execution_records(action_id,idempotency_key,status,"
        "amount_minor,created_at) VALUES($1,'rv_used0000000000000000000000000',"
        "'succeeded',1_982_200,$2)", aid, RUN_AT - timedelta(hours=4))

    with pytest.raises(ExecutionRefused) as e:
        await execute_action(conn, Fake(), action_id=aid, merchant_id="m_p",
                             settings=get_settings(), now=RUN_AT, policy_config=v4)
    assert e.value.rule == "daily_limit_exceeded"

    prov = await action_provenance(conn, aid)
    assert prov["authorization"]["authorized_policy_version"] == "v3"
    assert prov["authorization"]["decision"] == "AUTO_APPROVED"
    assert prov["execution"]["execution_policy_version"] == "v4"
    assert prov["execution"]["decision"] == "BLOCKED"
    assert prov["execution"]["rule"] == "daily_limit_exceeded"
    assert prov["policy_changed_between_phases"] is True
    assert prov["final_status"] == "NOT_EXECUTED"


async def test_hashes_differ_when_policy_changed(conn):
    v3 = _long_ttl(version="v3", max_daily_recovery_minor=2_500_000)
    v4 = _long_ttl(version="v4", max_daily_recovery_minor=2_000_000)
    aid = await _authorized_action(conn, config=v3)
    await conn.execute(
        "INSERT INTO execution_records(action_id,idempotency_key,status,"
        "amount_minor,created_at) VALUES($1,'rv_used1000000000000000000000000',"
        "'succeeded',1_982_200,$2)", aid, RUN_AT - timedelta(hours=4))
    with pytest.raises(ExecutionRefused):
        await execute_action(conn, Fake(), action_id=aid, merchant_id="m_p",
                             settings=get_settings(), now=RUN_AT, policy_config=v4)
    prov = await action_provenance(conn, aid)
    assert (prov["authorization"]["authorized_policy_hash"]
            != prov["execution"]["execution_policy_hash"])


async def test_unchanged_policy_is_flagged_as_unchanged(conn):
    """Same snapshot at both phases must NOT look like a policy change."""
    v3 = _long_ttl(version="v3")
    aid = await _authorized_action(conn, amount=10_000, config=v3)
    await execute_action(conn, Fake(), action_id=aid, merchant_id="m_p",
                         settings=get_settings(), now=RUN_AT, policy_config=v3)
    prov = await action_provenance(conn, aid)
    assert prov["policy_changed_between_phases"] is False
    assert prov["final_status"] == "EXECUTED"
    assert (prov["authorization"]["authorized_policy_hash"]
            == prov["execution"]["execution_policy_hash"])


# --- every refusal is explainable ------------------------------------------


async def test_expired_action_still_records_why(conn):
    """Regression: expiry was checked before the execution-phase evaluation, so
    an expired action left NO record of why money did not move."""
    short = PolicyConfig(version="v3", action_ttl_minutes=60)
    aid = await _authorized_action(conn, config=short)
    with pytest.raises(ExecutionRefused) as e:
        await execute_action(conn, Fake(), action_id=aid, merchant_id="m_p",
                             settings=get_settings(), now=RUN_AT,
                             policy_config=short)
    assert e.value.rule == "action_expired"
    prov = await action_provenance(conn, aid)
    assert prov["execution"] is not None, "a refusal with no recorded reason"
    assert prov["execution"]["rule"] == "action_expired"
    assert prov["final_status"] == "NOT_EXECUTED"


async def test_successful_execution_records_executed_at(conn):
    aid = await _authorized_action(conn, amount=10_000, config=_long_ttl(version="v3"))
    await execute_action(conn, Fake(), action_id=aid, merchant_id="m_p",
                         settings=get_settings(), now=RUN_AT,
                         policy_config=_long_ttl(version="v3"))
    prov = await action_provenance(conn, aid)
    assert prov["execution"]["executed_at"] is not None
    assert prov["execution"]["razorpay_ref"] == "plink_x"


# --- storage invariants ----------------------------------------------------


async def test_only_one_authorization_row_per_action(conn):
    """Enforced by a partial unique index, not by convention."""
    aid = await _authorized_action(conn)
    with pytest.raises(asyncpg.UniqueViolationError):
        await conn.execute(
            "INSERT INTO policy_decisions(action_id,phase,result,rule,reason,"
            "policy_version,policy_hash) VALUES($1,'authorization',"
            "'AUTO_APPROVED','r','x','v9','h')", aid)


async def test_multiple_execution_evaluations_allowed(conn):
    """An action may be evaluated for execution more than once — a retry after a
    transient failure — and every attempt belongs in the record."""
    aid = await _authorized_action(conn)
    for v in ("v4", "v5"):
        await conn.execute(
            "INSERT INTO policy_decisions(action_id,phase,result,rule,reason,"
            "policy_version,policy_hash) VALUES($1,'execution','BLOCKED','r','x',$2,'h')",
            aid, v)
    n = await conn.fetchval(
        "SELECT count(*) FROM policy_decisions WHERE action_id=$1 AND phase='execution'",
        aid)
    assert n == 2


# --- the hash itself -------------------------------------------------------


def test_hash_is_stable_and_value_sensitive():
    a = PolicyConfig(version="v3")
    assert policy_hash(a) == policy_hash(PolicyConfig(version="v3"))
    assert policy_hash(a) != policy_hash(PolicyConfig(version="v3",
                                                      max_daily_recovery_minor=1))
    # A version bump alone is still a different snapshot.
    assert policy_hash(a) != policy_hash(PolicyConfig(version="v4"))


def test_evaluation_phase_values():
    assert EvaluationPhase.AUTHORIZATION.value == "authorization"
    assert EvaluationPhase.EXECUTION.value == "execution"
