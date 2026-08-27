"""Investigation Agent.

Answers one question: **why is revenue being lost?**

It reads. It does not act. It holds no tool that can move money, approve
anything, or reach Razorpay — see `tools.py`, where that boundary is
structural rather than a matter of prompt wording. A prompt injection in a
failure_reason field can at worst make it reason badly; it cannot make it pay
anyone, because no such capability is in the graph.

Output is a typed object, never prose. Everything downstream — the Recovery
Agent, and eventually the Policy Engine — reads fields, not sentences. A model
that rambles produces a validation error, not an unpredictable financial
action.
"""

from __future__ import annotations

import logging

import asyncpg
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel, Field

from ..config import Settings
from .llm import build_model, invoke_with_validation, model_chain, resolve_model_name
from .tools import build_tools

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are the Investigation Agent for REVENANT, a revenue recovery system for \
Indian merchants using Razorpay.

Your job is to determine WHY revenue is being lost in a given incident. You \
investigate. You do not decide what to do about it, and you have no ability to \
act — another component handles recovery, and a deterministic policy engine \
decides what is permitted.

Method:
1. Start with get_incident_details to see what was detected.
2. Use get_merchant_baseline to establish what normal looks like.
3. Use get_failure_statistics to see whether the problem is confined to one \
payment method or one time window.
4. Use get_related_payments to inspect the actual failures and their reasons.
5. Only sample individual payments or customers if it would change your \
conclusion.

Rules:
- Ground every claim in tool output. If the data does not support a cause, say \
so and give a lower confidence rather than inventing a plausible story.
- Distinguish correlation from cause. A spike confined to one method during \
one window suggests an infrastructure problem with that method; failures \
spread evenly across methods and time suggests something else entirely.
- Never invent payment ids, amounts, error codes, or rates. Every figure must \
come from a tool result.
- Do not speculate about Razorpay's internal systems beyond what the failure \
codes actually say.
- Be concise. A merchant reads this, not an engineer.
"""


class InvestigationResult(BaseModel):
    """Structured finding. The Recovery Agent consumes these fields."""

    root_cause: str = Field(
        description="One or two sentences naming the most likely cause, grounded "
        "in the data. Plain language for a merchant."
    )
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="0-1. Use below 0.5 when the evidence is ambiguous. Do not "
        "inflate this to sound authoritative.",
    )
    affected_method: str | None = Field(
        default=None,
        description="Payment method most affected (upi/card/netbanking), or null "
        "if the problem is not method-specific.",
    )
    dominant_failure_reason: str | None = Field(
        default=None, description="Most common failure reason, verbatim from the data."
    )
    evidence: list[str] = Field(
        default_factory=list,
        description="Specific figures supporting the conclusion, each traceable "
        "to a tool result. E.g. 'UPI failure rate 83.3% vs 2.0% baseline'.",
    )
    is_transient: bool = Field(
        description="True if this looks like a temporary condition (timeout, "
        "gateway outage) where retrying may succeed. False if it looks "
        "structural (declined cards, insufficient funds) where a retry of the "
        "same payment would fail again. This materially affects whether "
        "recovery is worth attempting.",
    )
    recommended_focus: str = Field(
        description="Which subset of failures is most worth recovering, and why. "
        "A recommendation about focus only — NOT an action, NOT an amount.",
    )


def build_investigation_agent(
    pool: asyncpg.Pool, merchant_id: str, settings: Settings,
    model_name: str | None = None,
):
    """Construct the agent graph. Raises LLMUnavailable if unconfigured."""
    return create_react_agent(
        # temperature 0: investigation must be reproducible. Sampling variety
        # is not a virtue when the output feeds a financial pipeline.
        build_model(settings, temperature=0.0, model_name=model_name),
        tools=build_tools(pool, merchant_id),
        prompt=SYSTEM_PROMPT,
        response_format=InvestigationResult,
    )


async def investigate(
    pool: asyncpg.Pool, merchant_id: str, incident_id: int, settings: Settings
) -> tuple[InvestigationResult, int]:
    """Investigate one incident. Returns (result, tool_calls_made)."""
    # Passed as a factory so a daily-quota exhaustion can rebuild against the
    # next model instead of ending the run.
    def factory(model_name):
        return build_investigation_agent(pool, merchant_id, settings, model_name)

    # Validation is mandatory and retried. A malformed response raises rather
    # than propagating a half-parsed object into recovery logic.
    result, state = await invoke_with_validation(
        factory,
        [
            {
                "role": "user",
                "content": f"Investigate revenue incident {incident_id}. "
                "Determine the root cause.",
            }
        ],
        InvestigationResult,
        models=model_chain(settings),
    )
    tool_calls = sum(
        len(getattr(m, "tool_calls", []) or []) for m in state["messages"]
    )
    log.info(
        "investigation.complete incident=%s model=%s confidence=%.2f tools=%d",
        incident_id, resolve_model_name(settings), result.confidence, tool_calls,
    )
    return result, tool_calls
