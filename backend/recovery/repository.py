"""Persist a recovery plan.

Each candidate becomes a `recovery_actions` row plus the `policy_decisions`
row that ruled on it, written together in one transaction. An action without
its policy decision must never exist — the executor refuses to act on one, and
the audit trail could not explain it.

The action's `status` is derived from the policy decision, never set
independently. Two sources of truth for "may this execute?" is one too many.
"""

from __future__ import annotations

import json
from datetime import timedelta

import asyncpg

from ..policy import Decision, PolicyConfig
from .candidates import RecoveryCandidate, RecoveryPlan

#: policy decision -> initial action status
STATUS_FOR = {
    Decision.AUTO_APPROVED: "approved",
    Decision.REQUIRES_APPROVAL: "awaiting_approval",
    Decision.BLOCKED: "denied",
}


async def persist_plan(
    conn: asyncpg.Connection,
    *,
    merchant_id: str,
    incident_id: int,
    plan: RecoveryPlan,
    config: PolicyConfig | None = None,
) -> dict[str, int]:
    """Write a plan. Returns counts by outcome.

    Idempotent in practice: the `one_live_action_per_payment` exclusion
    constraint stops a second live action being created for a payment that
    already has one, so re-running planning cannot double up.
    """
    config = config or PolicyConfig()
    counts = {"created": 0, "skipped_existing": 0, "auto": 0,
              "approval": 0, "blocked": 0}

    async with conn.transaction():
        for candidate in plan.candidates:
            action_id = await _insert_action(
                conn, merchant_id=merchant_id, incident_id=incident_id,
                candidate=candidate, config=config,
            )
            if action_id is None:
                counts["skipped_existing"] += 1
                continue

            counts["created"] += 1
            if candidate.is_auto:
                counts["auto"] += 1
            elif candidate.needs_approval:
                counts["approval"] += 1
            else:
                counts["blocked"] += 1

        await conn.execute(
            """
            UPDATE revenue_incidents SET status='recovering'
             WHERE id=$1 AND status IN ('open','investigating')
            """,
            incident_id,
        )
    return counts


async def _insert_action(
    conn: asyncpg.Connection, *, merchant_id: str, incident_id: int,
    candidate: RecoveryCandidate, config: PolicyConfig,
) -> int | None:
    decision = candidate.decision
    status = STATUS_FOR[decision.decision]

    # The one_live_action_per_payment exclusion constraint only covers LIVE
    # statuses, so a terminal row (denied/failed/expired) does not hold the
    # slot. Without this guard, re-planning stacks a fresh 'denied' row for the
    # same payment on every run, and the audit trail fills with duplicate
    # records of the same refusal.
    already = await conn.fetchval(
        """
        SELECT 1 FROM recovery_actions
         WHERE incident_id = $1 AND payment_id = $2
         LIMIT 1
        """,
        incident_id, candidate.payment_id,
    )
    if already:
        return None

    # A blocked action still gets recorded — "we considered this and refused"
    # is exactly what an audit trail needs to show. But it is written in a
    # terminal state so it never occupies the one-live-action slot.
    action_id = await conn.fetchval(
        """
        INSERT INTO recovery_actions
            (incident_id, payment_id, customer_id, action, amount_paise,
             status, recovery_score, rationale, proposed_at, expires_at)
        VALUES ($1,$2,$3,$4::action_type,$5,$6::action_status,$7,$8,$9,$10)
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        incident_id,
        candidate.payment_id,
        candidate.customer_id,
        candidate.action.action.value,
        candidate.amount_paise,
        status,
        candidate.recovery_score,
        decision.reason,
        candidate.action.proposed_at,
        candidate.action.proposed_at + timedelta(minutes=config.action_ttl_minutes),
    )
    if action_id is None:
        return None

    await conn.execute(
        """
        INSERT INTO policy_decisions
            (action_id, phase, result, rule, reason, policy_version,
             policy_hash, metadata, evaluated_at)
        VALUES ($1,'authorization',$2::policy_result,$3,$4,$5,$6,$7,$8)
        """,
        action_id,
        decision.decision.value,
        decision.rule,
        decision.reason,
        decision.policy_version,
        decision.policy_hash,
        json.dumps(decision.metadata or {}),
        decision.evaluated_at,
    )

    await conn.execute(
        """
        INSERT INTO audit_events
            (actor, event_type, merchant_id, customer_id, payment_id,
             incident_id, action_id, amount_paise, policy_version,
             policy_result, reason, metadata)
        VALUES ('POLICY_ENGINE','POLICY_EVALUATED',$1,$2,$3,$4,$5,$6,$7,
                $8::policy_result,$9,$10)
        """,
        merchant_id,
        candidate.customer_id,
        candidate.payment_id,
        incident_id,
        action_id,
        candidate.amount_paise,
        decision.policy_version,
        decision.decision.value,
        decision.reason,
        json.dumps({"rule": decision.rule,
                    "recovery_score": candidate.recovery_score}),
    )
    return action_id
