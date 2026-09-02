"""Failure Lab — try the policy boundary yourself.

This is the one surface a visitor can poke at, so it is built to be SAFE TO
EXPOSE rather than hidden behind a flag:

* It calls the REAL Policy Engine — the same `evaluate()` the executor calls
  immediately before money moves. Not a simulation of the rules, the rules.
* It writes NOTHING and reaches Razorpay NEVER. Every scenario ends at a
  decision. There is no code path from here to a payment.
* It takes the merchant's real configured limits, so the numbers on screen are
  the ones actually enforced.

A policy simulator with its own copy of the rules would be worse than useless —
it would eventually disagree with production and reassure somebody wrongly.
This one cannot drift, because there is only one implementation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter
from pydantic import BaseModel, Field

from . import db
from .policy import (
    ActionType,
    EvaluationPhase,
    PaymentStatus,
    ProposedAction,
    RecoveryContext,
    evaluate,
    format_money,
    load_merchant_config,
)

router = APIRouter()
DEMO_MERCHANT = "m_demo"


class Scenario(BaseModel):
    """What a visitor can vary. Every field maps to a real policy input."""

    amount_minor: int = Field(default=93_900, ge=0, le=100_000_000)
    action: str = Field(default="CREATE_PAYMENT_LINK")
    payment_status: str = Field(default="failed")
    customer_opted_out: bool = False
    prior_attempts: int = Field(default=0, ge=0, le=10)
    minutes_since_last_attempt: int | None = Field(default=None, ge=0)
    already_recovered_today_minor: int = Field(default=0, ge=0)
    minutes_since_proposed: int = Field(default=0, ge=0)


#: The mandated failure scenarios, as one-click presets. Each names the rule it
#: is meant to trip, so a visitor can check the engine actually did what the
#: label claims.
PRESETS: dict[str, dict] = {
    "ordinary_recovery": {
        "label": "An ordinary recovery",
        "expect": "AUTO_APPROVED",
        "why": "Inside every limit. The system acts without asking anyone.",
        "scenario": {"amount_minor": 93_900},
    },
    "above_auto_limit": {
        "label": "Too much to decide alone",
        "expect": "REQUIRES_APPROVAL",
        "why": "Above the autonomous limit, so a human decides.",
        "scenario": {"amount_minor": 727_700},
    },
    "daily_cap": {
        "label": "The day's budget is spent",
        "expect": "BLOCKED",
        "why": "Today's recoveries already reached the cap.",
        "scenario": {"amount_minor": 300_000, "already_recovered_today_minor": 2_400_000},
    },
    "already_paid": {
        "label": "The customer already paid",
        "expect": "BLOCKED",
        "why": "Charging again would take money twice.",
        "scenario": {"amount_minor": 93_900, "payment_status": "captured"},
    },
    "opted_out": {
        "label": "The customer opted out",
        "expect": "BLOCKED",
        "why": "Checked before the amount — no sum makes contact acceptable.",
        "scenario": {"amount_minor": 93_900, "customer_opted_out": True},
    },
    "retry_limit": {
        "label": "Enough attempts",
        "expect": "BLOCKED",
        "why": "A stopping rule: two tries, then stop.",
        "scenario": {"amount_minor": 93_900, "prior_attempts": 2},
    },
    "cooldown": {
        "label": "Too soon after the last try",
        "expect": "BLOCKED",
        "why": "Chasing a customer minutes apart is harassment, not recovery.",
        "scenario": {"amount_minor": 93_900, "prior_attempts": 1,
                     "minutes_since_last_attempt": 5},
    },
    "stale_authorisation": {
        "label": "Approved a long time ago",
        "expect": "BLOCKED",
        "why": "Authorisation is not permission. Re-investigate before acting.",
        "scenario": {"amount_minor": 93_900, "minutes_since_proposed": 180},
    },
    "hallucinated_action": {
        "label": "The AI invents an action",
        "expect": "BLOCKED",
        "why": "An action the system does not define cannot be performed.",
        "scenario": {"amount_minor": 93_900, "action": "REFUND_EVERYTHING"},
    },
}


@router.get("/lab/scenarios")
async def scenarios():
    """The presets, with the merchant's real limits so the page can explain them."""
    async with db.pool().acquire() as conn:
        merchant = await load_merchant_config(conn, DEMO_MERCHANT)
    p = merchant.policy
    return {
        "currency": merchant.currency,
        "limits": {
            "auto_limit_minor": p.max_auto_amount_minor,
            "auto_limit": format_money(p.max_auto_amount_minor, merchant.currency),
            "daily_cap_minor": p.max_daily_recovery_minor,
            "daily_cap": format_money(p.max_daily_recovery_minor, merchant.currency),
            "max_retry_attempts": p.max_retry_attempts,
            "retry_cooldown_minutes": p.retry_cooldown_minutes,
            "action_ttl_minutes": p.action_ttl_minutes,
            "timezone": p.business_timezone,
            "policy_version": p.version,
        },
        "presets": [{"id": k, **v} for k, v in PRESETS.items()],
        "note": "These call the same Policy Engine the executor calls before "
                "money moves. Nothing here writes anything or reaches Razorpay.",
    }


@router.post("/lab/evaluate")
async def lab_evaluate(scenario: Scenario):
    """Run one scenario through the real Policy Engine.

    Pure: no writes, no Razorpay, no side effects of any kind.
    """
    now = datetime.now(timezone.utc)
    async with db.pool().acquire() as conn:
        merchant = await load_merchant_config(conn, DEMO_MERCHANT)

    try:
        action = ActionType(scenario.action)
    except ValueError:
        # Deliberately preserved: an unknown action is exactly what the
        # "AI invents an action" preset is testing, and the engine must be the
        # thing that refuses it — not this handler.
        action = scenario.action  # type: ignore[assignment]

    try:
        status = PaymentStatus(scenario.payment_status)
    except ValueError:
        status = PaymentStatus.FAILED

    proposed = ProposedAction(
        action=action,
        customer_id="cust_lab",
        payment_id="pay_lab",
        amount_minor=scenario.amount_minor,
        proposed_at=now - timedelta(minutes=scenario.minutes_since_proposed),
    )
    context = RecoveryContext(
        payment_status=status,
        customer_opted_out=scenario.customer_opted_out,
        prior_attempts=scenario.prior_attempts,
        last_attempt_at=(
            now - timedelta(minutes=scenario.minutes_since_last_attempt)
            if scenario.minutes_since_last_attempt is not None else None
        ),
        recovered_today_minor=scenario.already_recovered_today_minor,
        now=now,
    )

    decision = evaluate(proposed, context, merchant.policy,
                        phase=EvaluationPhase.EXECUTION)

    return {
        "decision": decision.decision.value,
        "rule": decision.rule,
        "reason": decision.reason,
        "policy_version": decision.policy_version,
        "policy_hash": decision.policy_hash,
        "currency": merchant.currency,
        "amount": format_money(scenario.amount_minor, merchant.currency),
        "would_move_money": decision.is_executable,
        "note": "Real Policy Engine. Nothing was written and no payment was made.",
    }
