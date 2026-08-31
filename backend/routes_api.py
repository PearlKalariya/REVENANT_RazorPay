"""Merchant-facing API.

Read endpoints are open (single-merchant demo) but PII-redacted. Mutating
endpoints — approve and deny — require the API key, because they authorise
money movement.

Every response that reports money distinguishes three things that are easy to
conflate and must not be:

    at risk    — detected, nothing done yet
    attempted  — a recovery action executed, outcome unknown
    recovered  — a verified Razorpay webhook confirmed payment

Only the third is revenue.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status

from . import db
from .config import get_settings
from .integrations.razorpay_client import RazorpayClient, RazorpayError
from .recovery.executor import ExecutionRefused, execute_action
from .recovery.provenance import action_provenance
from .redaction import redact
from .security import require_api_key

router = APIRouter()

DEMO_MERCHANT = "m_demo"


def _rupees(paise) -> str:
    return f"₹{int(paise or 0) / 100:,.2f}"


@router.get("/incidents")
async def list_incidents():
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT i.id, i.title, i.status::text AS status,
                   i.revenue_at_risk_paise, i.affected_count, i.detected_at,
                   inv.root_cause, inv.confidence
              FROM revenue_incidents i
              LEFT JOIN LATERAL (
                    SELECT root_cause, confidence FROM investigations
                     WHERE incident_id = i.id ORDER BY id DESC LIMIT 1
              ) inv ON TRUE
             WHERE i.merchant_id = $1
             ORDER BY i.revenue_at_risk_paise DESC
            """,
            DEMO_MERCHANT,
        )
    return {"incidents": [
        {
            "id": r["id"], "title": r["title"], "status": r["status"],
            "revenue_at_risk_paise": int(r["revenue_at_risk_paise"]),
            "revenue_at_risk": _rupees(r["revenue_at_risk_paise"]),
            "affected_count": r["affected_count"],
            "detected_at": r["detected_at"].isoformat(),
            "root_cause": r["root_cause"],
            "confidence": r["confidence"],
        } for r in rows
    ]}


@router.get("/incidents/{incident_id}")
async def get_incident(incident_id: int):
    async with db.pool().acquire() as conn:
        incident = await conn.fetchrow(
            """
            SELECT id, title, status::text AS status, revenue_at_risk_paise,
                   affected_count, detected_at, resolved_at
              FROM revenue_incidents WHERE id=$1 AND merchant_id=$2
            """, incident_id, DEMO_MERCHANT)
        if incident is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Incident not found")

        investigation = await conn.fetchrow(
            """
            SELECT root_cause, confidence, evidence, model, created_at
              FROM investigations WHERE incident_id=$1 ORDER BY id DESC LIMIT 1
            """, incident_id)
        strategy = await conn.fetchrow(
            """
            SELECT reason, metadata FROM audit_events
             WHERE incident_id=$1 AND event_type='RECOVERY_STRATEGY_PROPOSED'
             ORDER BY id DESC LIMIT 1
            """, incident_id)

    def as_dict(raw):
        return json.loads(raw) if isinstance(raw, str) else (raw or {})

    return redact({
        "id": incident["id"],
        "title": incident["title"],
        "status": incident["status"],
        "revenue_at_risk_paise": int(incident["revenue_at_risk_paise"]),
        "revenue_at_risk": _rupees(incident["revenue_at_risk_paise"]),
        "affected_count": incident["affected_count"],
        "detected_at": incident["detected_at"].isoformat(),
        "investigation": None if investigation is None else {
            "root_cause": investigation["root_cause"],
            "confidence": investigation["confidence"],
            "model": investigation["model"],
            **as_dict(investigation["evidence"]),
        },
        "strategy": None if strategy is None else {
            "rationale": strategy["reason"], **as_dict(strategy["metadata"]),
        },
    })


@router.get("/recovery-actions")
async def list_recovery_actions(status_filter: str | None = None):
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT ra.id, ra.payment_id, ra.customer_id, ra.action::text AS action,
                   ra.amount_paise, ra.status::text AS status, ra.recovery_score,
                   ra.rationale, ra.proposed_at, ra.expires_at,
                   pd.result::text AS policy_result, pd.rule AS policy_rule,
                   pd.policy_version AS authorized_policy_version,
                   er.status::text AS execution_status, er.razorpay_short_url,
                   ro.recovered_paise, ro.succeeded AS outcome_succeeded
              FROM recovery_actions ra
              JOIN revenue_incidents i ON i.id = ra.incident_id
              LEFT JOIN policy_decisions pd
                     ON pd.action_id = ra.id AND pd.phase = 'authorization'
              LEFT JOIN LATERAL (
                    SELECT status, razorpay_short_url, id FROM execution_records
                     WHERE action_id = ra.id ORDER BY id DESC LIMIT 1
              ) er ON TRUE
              LEFT JOIN recovery_outcomes ro ON ro.execution_id = er.id
             WHERE i.merchant_id = $1
               AND ($2::text IS NULL OR ra.status::text = $2)
             ORDER BY ra.recovery_score DESC NULLS LAST, ra.id
            """, DEMO_MERCHANT, status_filter)
    return {"actions": [
        {
            "id": r["id"], "payment_id": r["payment_id"],
            "customer_id": r["customer_id"], "action": r["action"],
            "amount_paise": int(r["amount_paise"]),
            "amount": _rupees(r["amount_paise"]),
            "status": r["status"],
            "recovery_score": r["recovery_score"],
            "rationale": r["rationale"],
            "policy": {
                "result": r["policy_result"], "rule": r["policy_rule"],
                "authorized_policy_version": r["authorized_policy_version"],
            },
            "execution_status": r["execution_status"],
            "payment_link": r["razorpay_short_url"],
            # Only a verified webhook produces this.
            "recovered_paise": int(r["recovered_paise"] or 0),
            "recovered": r["outcome_succeeded"] is True,
            "proposed_at": r["proposed_at"].isoformat(),
        } for r in rows
    ]}


@router.get("/recovery-actions/{action_id}/provenance")
async def get_provenance(action_id: int):
    """Full policy provenance (D15): why it was authorized, and why it did or
    did not execute."""
    async with db.pool().acquire() as conn:
        prov = await action_provenance(conn, action_id)
    if prov is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Action not found")
    return prov


async def _decide(action_id: int, approved: bool, approver: str, note: str | None):
    async with db.pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT ra.id, ra.status::text AS status, ra.amount_paise,
                   ra.customer_id, ra.payment_id, ra.incident_id
              FROM recovery_actions ra
              JOIN revenue_incidents i ON i.id = ra.incident_id
             WHERE ra.id=$1 AND i.merchant_id=$2
            """, action_id, DEMO_MERCHANT)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Action not found")
        if row["status"] != "awaiting_approval":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Action is {row['status']!r}, not awaiting approval.")

        async with conn.transaction():
            inserted = await conn.fetchval(
                """
                INSERT INTO approvals (action_id, approved, approver, note)
                VALUES ($1,$2,$3,$4)
                ON CONFLICT (action_id) DO NOTHING
                RETURNING id
                """, action_id, approved, approver, note)
            if inserted is None:
                # The unique constraint is the guarantee: an action can only be
                # decided once, so a double-click cannot approve twice.
                raise HTTPException(status.HTTP_409_CONFLICT,
                                    "Action has already been decided.")

            await conn.execute(
                "UPDATE recovery_actions SET status=$2::action_status WHERE id=$1",
                action_id, "approved" if approved else "denied")
            await conn.execute(
                """
                INSERT INTO audit_events
                    (actor, event_type, merchant_id, customer_id, payment_id,
                     incident_id, action_id, approval_id, amount_paise, reason)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                """,
                f"HUMAN:{approver}",
                "APPROVAL_GRANTED" if approved else "APPROVAL_DENIED",
                DEMO_MERCHANT, row["customer_id"], row["payment_id"],
                row["incident_id"], action_id, inserted,
                int(row["amount_paise"]), note or "")

    if not approved:
        return {
            "action_id": action_id, "approved": False, "approver": approver,
            "executed": False,
            "note": "Denied. No money will move for this payment.",
        }

    # An approval that does not lead anywhere is not an approval. Without this
    # the action sat at status 'approved' forever: no execution, no payment
    # link, nothing for a customer to pay — so recovered revenue could never
    # rise no matter how many approvals were granted.
    #
    # Executing here does NOT bypass anything. execute_action re-runs the
    # Policy Engine against current state first (D13), so an approval granted
    # this morning can still be refused now — a tightened cap, a customer who
    # opted out since, a payment already settled.
    settings = get_settings()
    try:
        client = RazorpayClient(settings)
    except RazorpayError as e:
        return {
            "action_id": action_id, "approved": True, "approver": approver,
            "executed": False, "note": f"Approved, but Razorpay is unavailable: {e}",
        }

    async with db.pool().acquire() as conn:
        try:
            outcome = await execute_action(
                conn, client, action_id=action_id,
                merchant_id=DEMO_MERCHANT, settings=settings)
        except ExecutionRefused as e:
            # The approval stands and is recorded; policy refused the movement.
            # Both facts are true and both belong in the response.
            return {
                "action_id": action_id, "approved": True, "approver": approver,
                "executed": False, "refused_rule": e.rule,
                "note": f"Approved, but policy refused execution: {e}",
            }
        except RazorpayError as e:
            return {
                "action_id": action_id, "approved": True, "approver": approver,
                "executed": False, "note": f"Approved. Razorpay error: {e}",
            }

    return {
        "action_id": action_id,
        "approved": True,
        "approver": approver,
        "executed": outcome.status == "succeeded",
        "execution_status": outcome.status,
        "payment_link": outcome.short_url,
        "note": (
            "Approved and payment link sent."
            if outcome.status == "succeeded"
            else f"Approved. Execution is {outcome.status} — "
                 "the outcome engine will resolve it."
        ),
    }


@router.post("/recovery-actions/{action_id}/approve",
             dependencies=[Depends(require_api_key)])
async def approve_action(action_id: int, request: Request,
                         body: dict = Body(default_factory=dict)):
    approver = (body.get("approver") or "").strip()
    if not approver:
        # Attribution is not optional for a financial approval.
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "An 'approver' identity is required.")
    return await _decide(action_id, True, approver, body.get("note"))


@router.post("/recovery-actions/{action_id}/deny",
             dependencies=[Depends(require_api_key)])
async def deny_action(action_id: int, request: Request,
                      body: dict = Body(default_factory=dict)):
    approver = (body.get("approver") or "").strip()
    if not approver:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "An 'approver' identity is required.")
    return await _decide(action_id, False, approver, body.get("note"))


@router.get("/audit")
async def audit_trail(limit: int = 100, action_id: int | None = None,
                      incident_id: int | None = None):
    limit = max(1, min(limit, 500))
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT ts, actor, event_type, customer_id, payment_id, incident_id,
                   action_id, execution_id, amount_paise, policy_version,
                   policy_result::text AS policy_result, reason, error, metadata
              FROM audit_events
             WHERE ($2::bigint IS NULL OR action_id = $2)
               AND ($3::bigint IS NULL OR incident_id = $3)
             ORDER BY id DESC LIMIT $1
            """, limit, action_id, incident_id)
    return redact({"events": [
        {
            "ts": r["ts"].isoformat(), "actor": r["actor"],
            "event_type": r["event_type"],
            "customer_id": r["customer_id"], "payment_id": r["payment_id"],
            "incident_id": r["incident_id"], "action_id": r["action_id"],
            "execution_id": r["execution_id"],
            "amount_paise": int(r["amount_paise"]) if r["amount_paise"] else None,
            "policy_version": r["policy_version"],
            "policy_result": r["policy_result"],
            "reason": r["reason"], "error": r["error"],
        } for r in rows
    ]})


@router.get("/metrics")
async def metrics():
    """Batch metrics.

    Every figure is labelled with what it actually measures. `recovered` counts
    ONLY verified Razorpay webhooks — a replayed event is excluded by the
    Outcome Engine, so this number cannot be inflated by anything the system
    generates itself.
    """
    async with db.pool().acquire() as conn:
        at_risk = int(await conn.fetchval(
            "SELECT coalesce(sum(amount_paise),0) FROM payments"
            " WHERE merchant_id=$1 AND status='failed'", DEMO_MERCHANT) or 0)
        actions = await conn.fetch(
            """
            SELECT ra.status::text AS status, count(*) n,
                   coalesce(sum(ra.amount_paise),0) paise
              FROM recovery_actions ra
              JOIN revenue_incidents i ON i.id = ra.incident_id
             WHERE i.merchant_id=$1 GROUP BY 1
            """, DEMO_MERCHANT)
        attempted = int(await conn.fetchval(
            "SELECT coalesce(sum(amount_paise),0) FROM execution_records"
            " WHERE status IN ('succeeded','pending')") or 0)
        recovered = int(await conn.fetchval(
            "SELECT coalesce(sum(recovered_paise),0) FROM recovery_outcomes"
            " WHERE succeeded") or 0)
        outcomes = int(await conn.fetchval(
            "SELECT count(*) FROM recovery_outcomes WHERE succeeded") or 0)
        blocked = await conn.fetch(
            """
            SELECT rule, count(*) n FROM policy_decisions
             WHERE result='BLOCKED' GROUP BY 1 ORDER BY 2 DESC
            """)
        links = int(await conn.fetchval(
            "SELECT count(*) FROM execution_records"
            " WHERE razorpay_ref IS NOT NULL") or 0)

    by_status = {r["status"]: {"count": r["n"], "paise": int(r["paise"])}
                 for r in actions}
    return {
        "data_source": "SYNTHETIC TEST DATA",
        "revenue_at_risk_paise": at_risk,
        "revenue_at_risk": _rupees(at_risk),
        "actions_by_status": by_status,
        "payment_links_issued": links,
        "recovery_attempted_paise": attempted,
        "recovery_attempted": _rupees(attempted),
        "recovered_paise": recovered,
        "recovered": _rupees(recovered),
        "recovered_count": outcomes,
        # Deliberately computed against ATTEMPTED, not at-risk: claiming a rate
        # against money never acted on would overstate the system's effect.
        "recovery_rate_of_attempted": (
            round(recovered / attempted, 4) if attempted else None),
        "policy_blocks": {r["rule"]: r["n"] for r in blocked},
        "recovered_definition":
            "Verified Razorpay webhooks only. Replayed events are excluded.",
    }
