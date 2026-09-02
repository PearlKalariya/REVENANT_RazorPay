"""Investigation Agent capability-boundary tests.

These are security tests, not functionality tests, and they deliberately need
no API key and no credits — the boundary must be verifiable independently of
whether the model can run at all.

The property under test: the Investigation Agent CANNOT move money, approve
anything, or write to the database, because no such capability exists in its
toolset. Prompt wording is not a security control; an absent function is.
"""

from __future__ import annotations

import inspect
import json
import re

import asyncpg
import pytest

from backend.agents import tools as tools_module
from backend.agents.tools import (
    ALLOWED_TOOL_NAMES,
    FORBIDDEN_CAPABILITIES,
    build_tools,
)
from backend.config import get_settings


@pytest.fixture
async def pool():
    try:
        p = await asyncpg.create_pool(
            get_settings().database_url, min_size=1, max_size=2, timeout=3
        )
    except Exception:
        pytest.skip("Postgres not reachable — run `docker compose up -d db`")
    try:
        yield p
    finally:
        await p.close()


# --- capability boundary --------------------------------------------------


async def test_agent_has_no_forbidden_capabilities(pool):
    """The core guarantee: no tool can execute, approve, refund, or pay."""
    names = {t.name for t in build_tools(pool, "m_demo")}
    assert names & FORBIDDEN_CAPABILITIES == set()


async def test_agent_tools_match_allowlist_exactly(pool):
    """A capability added by accident fails here rather than in production."""
    assert {t.name for t in build_tools(pool, "m_demo")} == ALLOWED_TOOL_NAMES


def test_no_write_sql_anywhere_in_toolset():
    """Structural: the tool module contains no mutating SQL.

    Read-only is enforced by what the code can express, not by intent. If
    someone later adds an INSERT to a 'read' tool, this fails immediately.
    """
    src = inspect.getsource(tools_module)
    # Strip docstrings/comments so prose about writes doesn't trip the check.
    code = re.sub(r'""".*?"""', "", src, flags=re.S)
    code = re.sub(r"#.*", "", code)
    for verb in ("INSERT", "UPDATE ", "DELETE", "DROP", "TRUNCATE", "ALTER"):
        assert verb not in code.upper(), f"Mutating SQL {verb!r} found in read-only tools"


def test_no_razorpay_import_in_toolset():
    """The Investigation Agent must have no path to the payment provider.

    Checks executable code only. The module docstring legitimately explains
    that the agent must not reach Razorpay, and prose saying so is not a
    capability.
    """
    src = inspect.getsource(tools_module)
    code = re.sub(r'\"\"\".*?\"\"\"', "", src, flags=re.S)
    code = re.sub(r"#.*", "", code)
    assert "razorpay" not in code.lower()
    assert "import razorpay" not in src


# --- merchant scoping -----------------------------------------------------


async def test_tools_are_scoped_to_one_merchant(pool):
    """merchant_id is bound at construction, not a tool argument, so the model
    cannot read another merchant's data by asking for it."""
    for t in build_tools(pool, "m_demo"):
        schema = t.args_schema.model_json_schema()
        assert "merchant_id" not in schema.get("properties", {}), (
            f"{t.name} exposes merchant_id as a model-controlled argument"
        )


async def test_unknown_merchant_returns_no_data(pool):
    tools = {t.name: t for t in build_tools(pool, "m_does_not_exist")}
    out = json.loads(await tools["get_incident_details"].coroutine(incident_id=1))
    assert "error" in out


async def test_cannot_read_another_merchants_incident(pool):
    """Incident 1 belongs to m_demo. A toolset bound elsewhere must not see it."""
    other = {t.name: t for t in build_tools(pool, "m_other")}
    out = json.loads(await other["get_incident_details"].coroutine(incident_id=1))
    assert "error" in out


# --- tool output contracts ------------------------------------------------


async def test_incident_details_returns_expected_fields(pool):
    tools = {t.name: t for t in build_tools(pool, "m_demo")}
    out = json.loads(await tools["get_incident_details"].coroutine(incident_id=1))
    if "error" in out:
        pytest.skip("no seeded incident — run detection first")
    for field in ("incident_id", "revenue_at_risk_minor", "affected_count",
                  "observed_failure_rate", "baseline_failure_rate"):
        assert field in out


async def test_baseline_returns_per_method_rates(pool):
    tools = {t.name: t for t in build_tools(pool, "m_demo")}
    out = json.loads(await tools["get_merchant_baseline"].coroutine())
    assert isinstance(out, list) and out
    for row in out:
        assert 0.0 <= row["failure_rate_including_incidents"] <= 1.0


async def test_customer_history_surfaces_opt_out(pool):
    """Opt-out must be visible to the model so its reasoning accounts for it —
    though it is the Policy Engine, not the model, that enforces it."""
    tools = {t.name: t for t in build_tools(pool, "m_demo")}
    out = json.loads(await tools["get_customer_history"].coroutine(customer_id="cust_0000"))
    if "error" in out:
        pytest.skip("customer not seeded")
    assert "opted_out_of_recovery" in out


async def test_all_tools_return_valid_json(pool):
    """Malformed tool output would corrupt the model's reasoning silently."""
    tools = {t.name: t for t in build_tools(pool, "m_demo")}
    calls = {
        "get_incident_details": {"incident_id": 1},
        "get_failure_statistics": {},
        "get_merchant_baseline": {},
        "get_related_payments": {"incident_id": 1},
        "get_payment_history": {"payment_id": "pay_SYN00001"},
        "get_customer_history": {"customer_id": "cust_0000"},
    }
    for name, kwargs in calls.items():
        json.loads(await tools[name].coroutine(**kwargs))


async def test_related_payments_limit_is_bounded(pool):
    """A model asking for 10_000 rows must not be able to pull the whole table."""
    tools = {t.name: t for t in build_tools(pool, "m_demo")}
    out = json.loads(await tools["get_related_payments"].coroutine(
        incident_id=1, limit=10_000))
    if isinstance(out, dict) and "error" in out:
        pytest.skip("no seeded incident")
    assert len(out) <= 100


# --- structured output contract -------------------------------------------


def test_investigation_result_rejects_invalid_confidence():
    """Structured output is validated, so a rambling model produces an error
    rather than an unpredictable downstream action."""
    from pydantic import ValidationError

    from backend.agents.investigation import InvestigationResult

    with pytest.raises(ValidationError):
        InvestigationResult(
            root_cause="x", confidence=1.5, is_transient=True,
            recommended_focus="y",
        )


def test_investigation_result_has_no_action_fields():
    """The Investigation Agent must not be able to express an action or an
    amount. Proposing recovery is a different agent's job."""
    from backend.agents.investigation import InvestigationResult

    fields = set(InvestigationResult.model_fields)
    for forbidden in ("action", "amount", "amount_minor", "approve",
                      "execute", "payment_link"):
        assert forbidden not in fields
