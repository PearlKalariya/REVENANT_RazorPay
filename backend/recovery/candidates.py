"""Expand a proposed strategy into per-payment recovery actions.

Deterministic. No LLM. This is the seam where AI judgement becomes concrete
money, and everything past this point is arithmetic and enforcement.

The agent said *which kind* of failure is worth recovering. This module finds
the matching payments, reads their real amounts from the database, and runs
EVERY resulting action through the Policy Engine independently.

Three properties this file is responsible for:

1. **Amounts come from the database, never from the model.** The agent has no
   field capable of expressing an amount, and nothing here reads one from it.
2. **Every candidate is policy-evaluated.** There is no path that produces an
   executable action without a PolicyDecision attached.
3. **The daily cap accumulates across the batch.** Evaluating each action
   against the cap in isolation would let a batch of individually-fine actions
   collectively blow through it — so the running total is threaded through.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import asyncpg

from ..policy import (
    ActionType,
    Decision,
    PolicyConfig,
    PolicyDecision,
    ProposedAction,
    PaymentStatus,
    RecoveryContext,
    evaluate,
)


@dataclass(frozen=True)
class RecoveryCandidate:
    payment_id: str
    customer_id: str
    amount_minor: int
    failure_reason: str | None
    method: str
    action: ProposedAction
    decision: PolicyDecision
    #: Advisory only. Never affects what policy permits (spec §24).
    recovery_score: float

    @property
    def is_auto(self) -> bool:
        return self.decision.decision is Decision.AUTO_APPROVED

    @property
    def needs_approval(self) -> bool:
        return self.decision.decision is Decision.REQUIRES_APPROVAL

    @property
    def is_blocked(self) -> bool:
        return self.decision.decision is Decision.BLOCKED


@dataclass
class RecoveryPlan:
    candidates: list[RecoveryCandidate]

    @property
    def auto(self) -> list[RecoveryCandidate]:
        return [c for c in self.candidates if c.is_auto]

    @property
    def approval(self) -> list[RecoveryCandidate]:
        return [c for c in self.candidates if c.needs_approval]

    @property
    def blocked(self) -> list[RecoveryCandidate]:
        return [c for c in self.candidates if c.is_blocked]

    @property
    def recoverable_minor(self) -> int:
        """Money that could move if every approval were granted. NOT a claim
        that it will be recovered."""
        return sum(c.amount_minor for c in self.auto + self.approval)

    def blocked_by_rule(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in self.blocked:
            out[c.decision.rule] = out.get(c.decision.rule, 0) + 1
        return out


def recovery_score(
    *, is_transient: bool, prior_attempts: int, customer_success_rate: float,
    age_hours: float,
) -> float:
    """Advisory likelihood that a recovery converts, 0-1.

    A transparent heuristic, deliberately not a trained model: there is no
    labelled outcome data, so anything "learned" here would be fitted to
    synthetic labels and would look more authoritative than it is.

    ADVISORY ONLY. Used for prioritisation. It never overrides the Policy
    Engine (spec §24).
    """
    score = 0.55 if is_transient else 0.18
    score += 0.25 * max(0.0, min(1.0, customer_success_rate))
    score -= 0.15 * min(prior_attempts, 3)
    # Intent decays. A customer who tried to pay an hour ago is a better bet
    # than one who tried three days ago.
    score -= 0.10 * min(age_hours / 72.0, 1.0)
    return round(max(0.0, min(1.0, score)), 3)


def _matches(reason: str | None, method: str, strategy) -> bool:
    if strategy.target_method and method != strategy.target_method:
        return False
    r = (reason or "").strip()
    if any(r == e.strip() for e in strategy.excluded_failure_reasons):
        return False
    if strategy.target_failure_reasons:
        return any(r == t.strip() for t in strategy.target_failure_reasons)
    return True


async def build_plan(
    conn: asyncpg.Connection,
    *,
    merchant_id: str,
    incident_id: int,
    strategy,
    is_transient: bool,
    config: PolicyConfig | None = None,
    now: datetime | None = None,
) -> RecoveryPlan:
    """Expand a strategy into policy-evaluated candidates."""
    # Merchant-owned limits, not a global default.
    if config is None:
        from ..policy import load_merchant_config
        config = (await load_merchant_config(conn, merchant_id)).policy
    now = now or datetime.now(timezone.utc)

    try:
        action_type = ActionType(strategy.normalized_action)
    except ValueError:
        # Fail closed on an unrecognised action rather than guessing which one
        # the model meant. Guessing here would guess about money.
        action_type = None

    rows = await conn.fetch(
        """
        SELECT p.id, p.customer_id, p.amount_minor, p.status::text AS status,
               p.method, p.failure_reason, p.created_at, c.opted_out,
               (SELECT count(*) FROM recovery_actions ra
                 WHERE ra.payment_id = p.id) AS prior_attempts,
               (SELECT max(ra.proposed_at) FROM recovery_actions ra
                 WHERE ra.payment_id = p.id) AS last_attempt_at,
               (SELECT count(*) FILTER (WHERE p2.status='captured')::float
                     / NULLIF(count(*), 0)
                  FROM payments p2 WHERE p2.customer_id = p.customer_id)
                 AS success_rate
          FROM payments p
          JOIN customers c ON c.id = p.customer_id
         WHERE p.merchant_id = $1 AND p.status = 'failed'
         ORDER BY p.amount_minor DESC, p.id
        """,
        merchant_id,
    )

    # Shared with the executor. Two different definitions of "spent today" is
    # how a cap gets breached while both sides believe they are within it.
    from .executor import daily_committed_minor

    recovered_today = await daily_committed_minor(
        conn, now, config.business_timezone, merchant_id)

    candidates: list[RecoveryCandidate] = []
    running_total = recovered_today

    for r in rows:
        if not _matches(r["failure_reason"], r["method"], strategy):
            continue

        amount = int(r["amount_minor"])
        proposed = ProposedAction(
            action=action_type or ActionType.CREATE_PAYMENT_LINK,
            customer_id=r["customer_id"],
            payment_id=r["id"],
            # Amount is read from the database. The model never supplies one.
            amount_minor=amount,
            proposed_at=now,
        )

        context = RecoveryContext(
            payment_status=PaymentStatus(r["status"]),
            customer_opted_out=r["opted_out"],
            prior_attempts=int(r["prior_attempts"] or 0),
            last_attempt_at=r["last_attempt_at"],
            # Threaded forward: a batch of individually-fine actions must not
            # collectively exceed the daily cap.
            recovered_today_minor=running_total,
            now=now,
        )

        if action_type is None:
            decision = PolicyDecision(
                decision=Decision.BLOCKED,
                rule="unknown_action",
                reason=f"Agent proposed unrecognised action "
                       f"{strategy.action_type!r}. Blocked.",
                policy_version=config.version,
                evaluated_at=now,
            )
        else:
            decision = evaluate(proposed, context, config)

        age_hours = max(
            0.0, (now - r["created_at"]).total_seconds() / 3600.0
        )
        candidates.append(
            RecoveryCandidate(
                payment_id=r["id"],
                customer_id=r["customer_id"],
                amount_minor=amount,
                failure_reason=r["failure_reason"],
                method=r["method"],
                action=proposed,
                decision=decision,
                recovery_score=recovery_score(
                    is_transient=is_transient,
                    prior_attempts=int(r["prior_attempts"] or 0),
                    customer_success_rate=float(r["success_rate"] or 0.0),
                    age_hours=age_hours,
                ),
            )
        )

        # Only actions that could actually execute consume the daily budget.
        if decision.decision is Decision.AUTO_APPROVED:
            running_total += amount

    return RecoveryPlan(candidates=candidates)
