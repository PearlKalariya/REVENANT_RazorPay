"""Merchant-facing API tests.

Two things get the most attention: financial authorisation on the mutating
endpoints, and PII never reaching a screen.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.config import get_settings
from backend.main import app
from backend.redaction import mask_email, mask_phone, redact


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def key():
    return get_settings().api_key


@pytest.fixture
def restore_decisions():
    """Undo any approval a test records.

    These tests drive the real app through TestClient, so unlike the asyncpg
    fixtures elsewhere there is no transaction to roll back — writes COMMIT.
    Running the suite was permanently consuming pending approvals and leaving
    the dashboard with fewer decisions than the batch actually produced.

    A test suite that quietly mutates the data it is run against is worse than
    no test: the state you inspect afterwards is not the state you had.
    """
    import asyncio

    import asyncpg

    async def snapshot():
        conn = await asyncpg.connect(get_settings().database_url, timeout=3)
        try:
            return (
                {r["action_id"] for r in await conn.fetch("SELECT action_id FROM approvals")},
                {r["id"]: r["status"] for r in await conn.fetch(
                    "SELECT id, status::text AS status FROM recovery_actions")},
            )
        finally:
            await conn.close()

    async def restore(before_approvals, before_status):
        conn = await asyncpg.connect(get_settings().database_url, timeout=3)
        try:
            await conn.execute(
                "DELETE FROM approvals WHERE action_id <> ALL($1::bigint[])",
                list(before_approvals))
            for action_id, status in before_status.items():
                await conn.execute(
                    "UPDATE recovery_actions SET status=$2::action_status"
                    " WHERE id=$1 AND status::text <> $2",
                    action_id, status)
            await conn.execute(
                "DELETE FROM audit_events"
                " WHERE event_type IN ('APPROVAL_GRANTED','APPROVAL_DENIED')"
                "   AND action_id <> ALL($1::bigint[])",
                list(before_approvals))
        finally:
            await conn.close()

    try:
        before = asyncio.run(snapshot())
    except Exception:
        pytest.skip("Postgres not reachable")
    yield
    asyncio.run(restore(*before))


# --- read endpoints -------------------------------------------------------


def test_incidents_listed(client):
    body = client.get("/incidents").json()
    assert "incidents" in body


def test_recovery_actions_listed(client):
    body = client.get("/recovery-actions").json()
    assert "actions" in body


def test_unknown_incident_404(client):
    assert client.get("/incidents/999999").status_code == 404


def test_unknown_provenance_404(client):
    assert client.get("/recovery-actions/999999/provenance").status_code == 404


# --- metrics honesty ------------------------------------------------------


def test_metrics_label_synthetic_data(client):
    """A number presented without saying it is synthetic is a number that will
    eventually be quoted as if it were real."""
    assert client.get("/metrics").json()["data_source"] == "SYNTHETIC TEST DATA"


def test_metrics_define_what_recovered_means(client):
    m = client.get("/metrics").json()
    assert "Verified Razorpay webhooks only" in m["recovered_definition"]
    assert "Replayed events are excluded" in m["recovered_definition"]


def test_metrics_separate_attempted_from_recovered(client):
    """Attempted and recovered are different facts. Conflating them is the
    easiest way to overstate what the system achieved."""
    m = client.get("/metrics").json()
    assert "recovery_attempted_paise" in m
    assert "recovered_paise" in m
    assert m["recovered_paise"] <= m["recovery_attempted_paise"]


def test_recovery_rate_is_against_attempted_not_at_risk(client):
    """A rate computed against at-risk money would credit the system for
    transactions it never acted on."""
    m = client.get("/metrics").json()
    if m["recovery_attempted_paise"]:
        expected = round(m["recovered_paise"] / m["recovery_attempted_paise"], 4)
        assert m["recovery_rate_of_attempted"] == expected


# --- approval authorisation -----------------------------------------------


def _awaiting(client):
    actions = client.get("/recovery-actions?status_filter=awaiting_approval").json()
    return actions["actions"][0]["id"] if actions["actions"] else None


def test_approve_requires_api_key(client):
    aid = _awaiting(client) or 1
    r = client.post(f"/recovery-actions/{aid}/approve", json={"approver": "x"})
    assert r.status_code == 401


def test_deny_requires_api_key(client):
    aid = _awaiting(client) or 1
    r = client.post(f"/recovery-actions/{aid}/deny", json={"approver": "x"})
    assert r.status_code == 401


def test_approval_requires_an_identified_approver(client, key, restore_decisions):
    """Attribution is not optional for a financial approval: an audit trail
    that cannot name who approved a payment is not an audit trail."""
    aid = _awaiting(client)
    if aid is None:
        pytest.skip("no action awaiting approval")
    r = client.post(f"/recovery-actions/{aid}/approve",
                    headers={"x-api-key": key}, json={})
    assert r.status_code == 400
    assert "approver" in r.text


def test_wrong_key_rejected(client):
    aid = _awaiting(client) or 1
    r = client.post(f"/recovery-actions/{aid}/approve",
                    headers={"x-api-key": "wrong"}, json={"approver": "x"})
    assert r.status_code == 401


def test_cannot_approve_an_action_not_awaiting_approval(client, key, restore_decisions):
    executed = client.get("/recovery-actions?status_filter=executed").json()["actions"]
    if not executed:
        pytest.skip("no executed action")
    r = client.post(f"/recovery-actions/{executed[0]['id']}/approve",
                    headers={"x-api-key": key}, json={"approver": "x"})
    assert r.status_code == 409


def test_approval_response_states_it_is_not_a_guarantee(client, key, restore_decisions):
    """Approval AUTHORISES; policy is re-evaluated at execution and may still
    refuse (D13). The response has to say so, or an approver will assume the
    money moved."""
    aid = _awaiting(client)
    if aid is None:
        pytest.skip("no action awaiting approval")
    r = client.post(f"/recovery-actions/{aid}/approve",
                    headers={"x-api-key": key},
                    json={"approver": "test-human"})
    if r.status_code == 200:
        assert "re-evaluated" in r.json()["note"]


# --- PII ------------------------------------------------------------------


def test_email_and_phone_are_masked():
    assert mask_email("someone@example.com") == "s***@example.com"
    assert mask_phone("+918128139249") == "+91******49"


def test_sensitive_keys_are_redacted_at_any_depth():
    out = redact({"a": {"b": {"card": {"last4": "1111"}, "email": "x.y@z.com"}}})
    assert out["a"]["b"]["card"] == "[redacted]"
    assert out["a"]["b"]["email"] == "x***@z.com"


def test_no_endpoint_leaks_a_raw_email(client):
    """Real webhook payloads carry customer contact details, and a recovery
    dashboard gets screen-shared."""
    for path in ("/incidents", "/recovery-actions", "/audit?limit=200",
                 "/metrics"):
        assert "@gmail.com" not in client.get(path).text
        assert "@example.test" not in client.get(path).text


def test_audit_events_are_redacted(client):
    body = client.get("/audit?limit=200").text
    import re
    assert not re.search(r"[A-Za-z0-9._%+-]{2,}@[A-Za-z0-9.-]+", body)
