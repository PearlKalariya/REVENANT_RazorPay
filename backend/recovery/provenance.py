"""Policy provenance for a recovery action (D15).

Answers the three questions an auditor asks:

1. Why was it originally allowed?      -> the authorization evaluation
2. Why wasn't it executed?             -> the execution evaluation
3. Was the newer policy correctly applied?
                                       -> both snapshots are pinned by hash,
                                          so the execution-time evaluation is
                                          reproducible

Authorization and execution are separate rows. Neither overwrites the other,
so an action refused today still carries the full record of why it was
authorised yesterday.
"""

from __future__ import annotations

import json

import asyncpg

#: Terminal states, derived rather than stored, so it cannot drift out of step
#: with the rows it summarises.
FINAL_EXECUTED = "EXECUTED"
FINAL_NOT_EXECUTED = "NOT_EXECUTED"
FINAL_PENDING = "PENDING"
FINAL_AWAITING = "AWAITING_AUTHORIZATION"


async def action_provenance(conn: asyncpg.Connection, action_id: int) -> dict | None:
    """Full policy provenance for one action, or None if unknown."""
    action = await conn.fetchrow(
        """
        SELECT ra.id, ra.amount_paise, ra.status::text AS status,
               ra.payment_id, ra.customer_id, ra.action::text AS action
          FROM recovery_actions ra WHERE ra.id = $1
        """,
        action_id,
    )
    if action is None:
        return None

    rows = await conn.fetch(
        """
        SELECT phase::text AS phase, result::text AS result, rule, reason,
               policy_version, policy_hash, evaluated_at, metadata
          FROM policy_decisions
         WHERE action_id = $1
         ORDER BY id
        """,
        action_id,
    )
    # Only executions that carry execution-phase policy provenance. A row
    # without it did not come from the executor's gated path, and treating it
    # as evidence of a real money movement would misreport final_status.
    execution = await conn.fetchrow(
        """
        SELECT status::text AS status, razorpay_ref, executed_at,
               execution_policy_version, execution_policy_hash,
               execution_policy_evaluated_at
          FROM execution_records
         WHERE action_id = $1
           AND execution_policy_hash IS NOT NULL
         ORDER BY id DESC LIMIT 1
        """,
        action_id,
    )

    def phase_row(name: str):
        # Last evaluation of that phase: an action can be evaluated for
        # execution more than once, and the latest is what decided the outcome.
        matching = [r for r in rows if r["phase"] == name]
        return matching[-1] if matching else None

    auth = phase_row("authorization")
    exec_eval = phase_row("execution")

    if execution is not None and execution["status"] == "succeeded":
        final = FINAL_EXECUTED
    elif execution is not None and execution["status"] == "pending":
        final = FINAL_PENDING
    elif exec_eval is not None:
        final = FINAL_NOT_EXECUTED
    elif auth is not None and auth["result"] == "REQUIRES_APPROVAL":
        final = FINAL_AWAITING
    else:
        final = FINAL_NOT_EXECUTED

    return {
        "action_id": action["id"],
        "payment_id": action["payment_id"],
        "customer_id": action["customer_id"],
        "amount_paise": int(action["amount_paise"]),
        # Explicit field names throughout: a generic "policy_version" would be
        # ambiguous about WHICH evaluation it describes.
        "authorization": None if auth is None else {
            "authorized_policy_version": auth["policy_version"],
            "authorized_policy_hash": auth["policy_hash"],
            "decision": auth["result"],
            "rule": auth["rule"],
            "reason": auth["reason"],
            "authorized_at": auth["evaluated_at"].isoformat(),
        },
        "execution": None if exec_eval is None else {
            "execution_policy_version": exec_eval["policy_version"],
            "execution_policy_hash": exec_eval["policy_hash"],
            "decision": exec_eval["result"],
            "rule": exec_eval["rule"],
            "reason": exec_eval["reason"],
            "execution_policy_evaluated_at": exec_eval["evaluated_at"].isoformat(),
            "executed_at": (
                execution["executed_at"].isoformat()
                if execution is not None and execution["executed_at"] else None
            ),
            "razorpay_ref": execution["razorpay_ref"] if execution else None,
        },
        # True when the two evaluations ran against different policy snapshots.
        # This is the flag that explains "approved yesterday, refused today".
        "policy_changed_between_phases": bool(
            auth and exec_eval and auth["policy_hash"] != exec_eval["policy_hash"]
        ),
        "final_status": final,
    }
