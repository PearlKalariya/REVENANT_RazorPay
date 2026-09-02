"""Recovery Strategy Agent.

Answers: **what should we do about it?**

It proposes. It cannot execute, cannot approve, and cannot bypass policy.

The important design choice is what the model is asked for. It does NOT emit
35 individual payment actions. It emits ONE strategy — which segment of
failures is worth recovering, which action type, and which failures to skip —
and deterministic code expands that into per-payment actions, each of which is
independently evaluated by the Policy Engine.

That split matters:

* The model does judgement: is a UPI timeout worth retrying? Is an insufficient
  funds decline a waste of a payment link?
* Deterministic code does arithmetic and enforcement: who matches, how much,
  and what policy says.

A model that emits per-transaction amounts is a model that can get an amount
wrong. Here it never touches one. Amounts come from the database, and every
resulting action passes through the Policy Engine before anything can happen.
"""

from __future__ import annotations

import logging

import asyncpg
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel, Field

from ..config import Settings
from .investigation import InvestigationResult
from .llm import (
    build_model,
    invoke_structured,
    make_submit_tool,
    model_chain,
    resolve_model_name,
)
from .tools import build_tools

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are the Recovery Strategy Agent for REVENANT, a revenue recovery system \
for Indian merchants using Razorpay.

An investigation has already determined why revenue was lost. Your job is to \
decide WHICH failed payments are worth trying to recover, and HOW.

You propose a strategy. You do not execute anything. A deterministic policy \
engine independently checks every resulting action against the merchant's \
limits, and it can and will override you. Do not attempt to work around it, \
and do not reference limits as if you were enforcing them.

Available actions:
- CREATE_PAYMENT_LINK: send the customer a fresh Razorpay payment link. Real \
money, worth using when the customer plausibly still wants to pay.
- SEND_RECOVERY_NOTIFICATION: a reminder with no payment link. Cheaper and \
lower friction, but lower conversion.

How to think about it:
- Transient failures (timeouts, expired collect requests, gateway outages) are \
worth recovering. The customer intended to pay and the infrastructure failed \
them.
- Structural failures (insufficient funds, card declined by the issuer, expired \
or closed cards, international card not supported) will fail again for the same \
reason. A payment link sent to somebody whose card was declined for lack of \
funds wastes effort and irritates a customer who already knows.
- **Declining is a real answer.** If the failures in this incident are \
predominantly structural, set worth_recovering to false and say why. Do not \
manufacture a strategy to look useful. An incident where the right action is \
"do nothing" is a correct outcome, and reporting it honestly is worth more to \
the merchant than a doomed recovery attempt.
- Look at the ACTUAL failure reasons before deciding. Do not assume a spike \
means recoverable: a surge of declines from one issuing bank is not the same \
problem as a gateway outage, and the two need opposite responses.
- Do not propose recovery for every failure just to maximise the number.

Use the tools to ground your reasoning. Check the merchant baseline and the \
actual failure reasons before deciding. Never invent figures.

When you have decided, call `submit_findings` exactly once with your strategy. \
That call is how you deliver your proposal — nothing else is recorded.
"""


class RecoveryStrategy(BaseModel):
    """A proposed strategy. Deterministic code expands this into actions.

    Deliberately contains NO payment ids and NO amounts: the model must not be
    able to specify who gets charged what. Those come from the database.
    """

    worth_recovering: bool = Field(
        description="False when these failures should NOT be recovered at all — "
        "structural declines (insufficient funds, closed or expired cards, a "
        "bank refusing the transaction) that will fail again for the same "
        "reason. Declining is a real answer and often the correct one: a "
        "payment link sent to somebody whose card was declined for lack of "
        "funds wastes effort and irritates a customer who already knows.",
    )
    action_type: str = Field(
        description="CREATE_PAYMENT_LINK or SEND_RECOVERY_NOTIFICATION. "
        "Ignored when worth_recovering is false."
    )
    target_failure_reasons: list[str] = Field(
        default_factory=list,
        description="Failure reasons WORTH recovering, verbatim as they appear "
        "in the data. Empty means all reasons not explicitly excluded.",
    )
    excluded_failure_reasons: list[str] = Field(
        default_factory=list,
        description="Failure reasons to SKIP because a retry would likely fail "
        "for the same cause.",
    )
    target_method: str | None = Field(
        default=None,
        description="Restrict to one payment method (upi/card/netbanking), or "
        "null for all.",
    )
    rationale: str = Field(
        description="Two or three sentences a merchant would understand, "
        "explaining why this segment and this action."
    )
    expected_recovery_rate: float = Field(
        ge=0.0, le=1.0,
        description="Advisory estimate of the fraction that will convert. This "
        "is an estimate for prioritisation only and never affects what policy "
        "permits. Be realistic, not optimistic.",
    )
    confidence: float = Field(
        ge=0.0, le=1.0, description="Confidence in this strategy, 0-1."
    )

    @property
    def normalized_action(self) -> str:
        return self.action_type.strip().upper()


def build_recovery_agent(pool: asyncpg.Pool, merchant_id: str, settings: Settings,
                         model_name: str | None = None):
    return create_react_agent(
        build_model(settings, temperature=0.0, model_name=model_name),
        # Same READ-ONLY toolset, plus the terminal submission tool. Note that
        # submit_findings carries no execution capability — it records a
        # proposal, which the Policy Engine then rules on.
        tools=[*build_tools(pool, merchant_id), make_submit_tool(RecoveryStrategy)],
        prompt=SYSTEM_PROMPT,
    )


async def propose_strategy(
    pool: asyncpg.Pool,
    merchant_id: str,
    incident_id: int,
    investigation: InvestigationResult,
    settings: Settings,
) -> tuple[RecoveryStrategy, int]:
    """Propose a recovery strategy for an investigated incident."""
    def factory(model_name):
        return build_recovery_agent(pool, merchant_id, settings, model_name)

    brief = (
        f"Incident {incident_id} has been investigated.\n\n"
        f"Root cause: {investigation.root_cause}\n"
        f"Investigator confidence: {investigation.confidence}\n"
        f"Affected method: {investigation.affected_method}\n"
        f"Dominant failure reason: {investigation.dominant_failure_reason}\n"
        f"Assessed as transient: {investigation.is_transient}\n"
        f"Investigator's recommended focus: {investigation.recommended_focus}\n\n"
        "Propose a recovery strategy. Inspect the actual failures before "
        "deciding which are worth recovering."
    )

    strategy, state = await invoke_structured(
        factory, [{"role": "user", "content": brief}], RecoveryStrategy,
        models=model_chain(settings),
    )
    tool_calls = sum(len(getattr(m, "tool_calls", []) or []) for m in state["messages"])

    log.info(
        "recovery.strategy incident=%s action=%s confidence=%.2f tools=%d",
        incident_id, strategy.normalized_action, strategy.confidence, tool_calls,
    )
    return strategy, tool_calls
