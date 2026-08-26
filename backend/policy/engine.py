"""REVENANT Policy Engine.

The deterministic financial safety boundary. Every proposed recovery action
passes through `evaluate()` before it can reach the executor.

Design rules, in priority order:

1. **Deterministic.** No LLM, no randomness, no I/O, no clock access. The same
   inputs always produce the same decision. `now` is passed in by the caller so
   evaluation is reproducible and testable.
2. **Fails closed.** Anything unrecognised, malformed, or out of range is
   BLOCKED. An agent that emits garbage gets a refusal, never a pass.
3. **Blocks before approvals.** Hard blocks are evaluated before the amount
   threshold, so an opted-out customer is BLOCKED rather than escalated to a
   human who might wave it through.
4. **First match wins**, and the decision records which rule fired, so the audit
   ledger can always answer "why".

Money is integer paise everywhere (decision D8). Never floats.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

POLICY_VERSION = "v1"


class Decision(str, Enum):
    AUTO_APPROVED = "AUTO_APPROVED"
    REQUIRES_APPROVAL = "REQUIRES_APPROVAL"
    BLOCKED = "BLOCKED"


class ActionType(str, Enum):
    CREATE_PAYMENT_LINK = "CREATE_PAYMENT_LINK"
    SEND_RECOVERY_NOTIFICATION = "SEND_RECOVERY_NOTIFICATION"


#: Actions that move money. These are subject to amount and daily-cap rules.
FINANCIAL_ACTIONS = frozenset({ActionType.CREATE_PAYMENT_LINK})


class PaymentStatus(str, Enum):
    """Payment states REVENANT understands.

    Only FAILED is recoverable. Everything else is a block — most importantly
    CAPTURED, which is the already-paid case.
    """

    CREATED = "created"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    FAILED = "failed"
    REFUNDED = "refunded"


RECOVERABLE_STATUSES = frozenset({PaymentStatus.FAILED})


@dataclass(frozen=True)
class PolicyConfig:
    """Merchant recovery policy. Frozen: a decision is evaluated against exactly
    one config, and the version is stamped onto the result for audit."""

    max_auto_amount_paise: int = 500_000  # ₹5,000
    max_daily_recovery_paise: int = 2_500_000  # ₹25,000
    max_retry_attempts: int = 2
    retry_cooldown_minutes: int = 30
    action_ttl_minutes: int = 60
    duplicate_payment_check: bool = True
    enforce_customer_opt_out: bool = True
    version: str = POLICY_VERSION


@dataclass(frozen=True)
class ProposedAction:
    """A recovery action proposed by the Recovery Strategy Agent.

    This is the ONLY shape the agent can emit. It cannot express a refund, a
    payout, a policy change, or an approval — those capabilities do not exist
    in this type, so the agent cannot ask for them.
    """

    action: ActionType
    customer_id: str
    payment_id: str
    amount_paise: int
    proposed_at: datetime


@dataclass(frozen=True)
class RecoveryContext:
    """Authoritative facts, read from the database — never from the LLM."""

    payment_status: PaymentStatus
    customer_opted_out: bool
    prior_attempts: int
    recovered_today_paise: int
    now: datetime
    last_attempt_at: datetime | None = None


@dataclass(frozen=True)
class PolicyDecision:
    decision: Decision
    rule: str
    reason: str
    policy_version: str
    evaluated_at: datetime
    metadata: dict = field(default_factory=dict)

    @property
    def is_executable(self) -> bool:
        """True only if the executor may act without further human input."""
        return self.decision is Decision.AUTO_APPROVED


def _rupees(paise: int) -> str:
    """Format paise for human-readable reasons. Display only."""
    return f"₹{paise / 100:,.2f}".rstrip("0").rstrip(".")


def evaluate(
    action: ProposedAction,
    context: RecoveryContext,
    config: PolicyConfig | None = None,
) -> PolicyDecision:
    """Evaluate a proposed action against merchant policy.

    Returns exactly one of AUTO_APPROVED, REQUIRES_APPROVAL, or BLOCKED.
    Never raises on bad input — malformed input is BLOCKED, because a financial
    boundary that throws is a financial boundary that someone will wrap in a
    try/except and accidentally bypass.
    """
    config = config or PolicyConfig()
    now = context.now

    def decide(decision: Decision, rule: str, reason: str, **meta) -> PolicyDecision:
        return PolicyDecision(
            decision=decision,
            rule=rule,
            reason=reason,
            policy_version=config.version,
            evaluated_at=now,
            metadata=meta,
        )

    def block(rule: str, reason: str, **meta) -> PolicyDecision:
        return decide(Decision.BLOCKED, rule, reason, **meta)

    # --- Structural validation. Fail closed. -------------------------------

    if not isinstance(action.action, ActionType):
        return block(
            "unknown_action",
            f"Unrecognised action type {action.action!r}. Blocked by default.",
        )

    if not action.customer_id or not action.payment_id:
        return block(
            "missing_identifiers",
            "Action is missing customer_id or payment_id.",
        )

    is_financial = action.action in FINANCIAL_ACTIONS

    if is_financial:
        if not isinstance(action.amount_paise, int) or isinstance(
            action.amount_paise, bool
        ):
            return block(
                "invalid_amount",
                "Amount must be an integer number of paise.",
            )
        if action.amount_paise <= 0:
            return block(
                "invalid_amount",
                f"Amount must be positive, got {action.amount_paise} paise.",
                amount_paise=action.amount_paise,
            )

    # --- Action freshness. An agent proposal is not valid forever. ---------

    age = now - action.proposed_at
    if age > timedelta(minutes=config.action_ttl_minutes):
        return block(
            "action_expired",
            f"Action was proposed {int(age.total_seconds() // 60)} minutes ago, "
            f"exceeding the {config.action_ttl_minutes} minute limit. "
            "Re-investigate before acting.",
            age_minutes=int(age.total_seconds() // 60),
        )
    if age < timedelta(0):
        return block(
            "action_from_future",
            "Action timestamp is in the future. Refusing to evaluate.",
        )

    # --- Hard blocks. Evaluated before any approval path. ------------------

    if config.enforce_customer_opt_out and context.customer_opted_out:
        return block(
            "customer_opted_out",
            f"Customer {action.customer_id} has opted out of recovery contact.",
        )

    if config.duplicate_payment_check:
        if context.payment_status is PaymentStatus.CAPTURED:
            return block(
                "already_paid",
                f"Payment {action.payment_id} is already captured. "
                "Recovery would double-charge the customer.",
                payment_status=context.payment_status.value,
            )
        if context.payment_status not in RECOVERABLE_STATUSES:
            return block(
                "payment_not_recoverable",
                f"Payment {action.payment_id} is {context.payment_status.value}, "
                "which is not a recoverable state.",
                payment_status=context.payment_status.value,
            )

    if context.prior_attempts >= config.max_retry_attempts:
        return block(
            "retry_limit_exceeded",
            f"{context.prior_attempts} recovery attempts already made, "
            f"limit is {config.max_retry_attempts}. Stopping.",
            prior_attempts=context.prior_attempts,
        )

    if context.last_attempt_at is not None:
        elapsed = now - context.last_attempt_at
        cooldown = timedelta(minutes=config.retry_cooldown_minutes)
        if elapsed < cooldown:
            remaining = int((cooldown - elapsed).total_seconds() // 60) + 1
            return block(
                "cooldown_active",
                f"Last attempt was under {config.retry_cooldown_minutes} minutes "
                f"ago. Retry available in ~{remaining} minute(s).",
                minutes_remaining=remaining,
            )

    # --- Money-moving rules. Non-financial actions skip these. -------------

    if is_financial:
        projected = context.recovered_today_paise + action.amount_paise
        if projected > config.max_daily_recovery_paise:
            return block(
                "daily_limit_exceeded",
                f"This action would bring today's recovery total to "
                f"{_rupees(projected)}, above the "
                f"{_rupees(config.max_daily_recovery_paise)} daily cap.",
                projected_paise=projected,
                cap_paise=config.max_daily_recovery_paise,
            )

        if action.amount_paise > config.max_auto_amount_paise:
            return decide(
                Decision.REQUIRES_APPROVAL,
                "above_auto_threshold",
                f"Amount {_rupees(action.amount_paise)} exceeds the "
                f"{_rupees(config.max_auto_amount_paise)} autonomous limit. "
                "Human approval required.",
                amount_paise=action.amount_paise,
                threshold_paise=config.max_auto_amount_paise,
            )

    # --- All checks passed. -------------------------------------------------

    return decide(
        Decision.AUTO_APPROVED,
        "within_policy",
        f"{action.action.value} for {_rupees(action.amount_paise)} is within all "
        "policy limits."
        if is_financial
        else f"{action.action.value} is non-financial and within policy.",
        amount_paise=action.amount_paise,
    )
