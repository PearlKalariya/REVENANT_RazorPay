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

import hashlib
import json
from dataclasses import dataclass, field, fields
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
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

    #: ISO-4217 currency this merchant settles in. Amounts are always stored
    #: in the currency's MINOR unit (paise, cents, pence) as integers — the
    #: field names say `paise` for historical reasons but the unit is whatever
    #: minor unit `currency` implies.
    currency: str = "INR"

    #: Timezone the merchant's "day" is measured in.
    #:
    #: There is no correct global default here, which is exactly why it belongs
    #: to the merchant. A UTC day rolls over at 05:30 IST, 19:00 the previous
    #: day in New York, and 01:00 in London — so a UTC-based daily cap resets in
    #: the middle of somebody's business day no matter which default is chosen.
    #: The LIMIT is the merchant's; so is the day it applies to.
    business_timezone: str = "Asia/Kolkata"

    max_auto_amount_minor: int = 500_000  # ₹5,000
    max_daily_recovery_minor: int = 2_500_000  # ₹25,000
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
    amount_minor: int
    proposed_at: datetime


@dataclass(frozen=True)
class RecoveryContext:
    """Authoritative facts, read from the database — never from the LLM."""

    payment_status: PaymentStatus
    customer_opted_out: bool
    prior_attempts: int
    recovered_today_minor: int
    now: datetime
    last_attempt_at: datetime | None = None


def policy_hash(config: "PolicyConfig") -> str:
    """Stable fingerprint of a complete policy snapshot.

    A version string alone is not enough provenance. Versions get reused,
    migrated, or renamed, and "v3" in a year's time may not describe the same
    rules it described today. The hash pins the ACTUAL values an evaluation ran
    against, so an auditor can prove two evaluations used identical policy even
    if the representation changed underneath.

    Computed over sorted field names, so adding a field changes the hash (it is
    a different policy) but reordering the dataclass does not.
    """
    payload = json.dumps(
        {f.name: getattr(config, f.name) for f in fields(config)},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


class EvaluationPhase(str, Enum):
    """When an evaluation happened.

    AUTHORIZATION and EXECUTION are two separate facts about an action and must
    never overwrite each other: one records why it was allowed to be planned,
    the other why money did or did not move.
    """

    AUTHORIZATION = "authorization"
    EXECUTION = "execution"


@dataclass(frozen=True)
class PolicyDecision:
    decision: Decision
    rule: str
    reason: str
    policy_version: str
    #: Fingerprint of the exact policy snapshot this decision was made against.
    policy_hash: str
    evaluated_at: datetime
    phase: EvaluationPhase = EvaluationPhase.AUTHORIZATION
    metadata: dict = field(default_factory=dict)

    @property
    def is_executable(self) -> bool:
        """True only if the executor may act without further human input."""
        return self.decision is Decision.AUTO_APPROVED


def current_merchant_day(now: datetime, timezone_name: str) -> date:
    """The merchant's current business date.

    Not the UTC date. A merchant's daily limit resets at midnight where they
    trade, not at midnight UTC — those are different instants everywhere except
    a narrow band of longitudes.

    Pure and deterministic: `now` is supplied by the caller, never read from a
    clock inside the policy layer.
    """
    try:
        tz = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        # Fail LOUD rather than silently falling back to UTC. A silent fallback
        # would move the day boundary for a merchant without anyone noticing,
        # and the symptom would be an over-spent daily cap weeks later.
        raise ValueError(
            f"Unknown business_timezone {timezone_name!r}. "
            "Use an IANA name such as 'Asia/Kolkata' or 'America/New_York'."
        ) from None
    return now.astimezone(tz).date()


def merchant_day_start(now: datetime, timezone_name: str) -> datetime:
    """The instant the merchant's current business day began, as an aware UTC
    datetime. This is the lower bound for "spent today"."""
    tz = ZoneInfo(timezone_name)          # validated by current_merchant_day
    local_midnight = datetime.combine(
        current_merchant_day(now, timezone_name), time.min, tzinfo=tz
    )
    return local_midnight.astimezone(UTC)


def _rupees(amount_minor: int) -> str:
    """Deprecated alias kept for readability at existing call sites."""
    return format_money(amount_minor)

#: Minor units per major unit. Most currencies are 100; the exceptions are real
#: (JPY has no minor unit, KWD has 1000) and getting them wrong misprices by
#: a factor of 100 or more.
MINOR_PER_MAJOR = {"INR": 100, "USD": 100, "GBP": 100, "EUR": 100,
                   "JPY": 1, "KWD": 1000, "BHD": 1000}
SYMBOLS = {"INR": "\u20b9", "USD": "$", "GBP": "\u00a3", "EUR": "\u20ac", "JPY": "\u00a5"}


def format_money(amount_minor: int | None, currency: str = "INR") -> str:
    """Render a minor-unit integer in its own currency.

    The unit is defined by the CURRENCY, never by the field name — which is why
    the fields are `*_minor` and not `*_paise` (decision D16). 500_000 is
    \u20b95,000.00 in INR and $5,000.00 in USD, and the code must not assume which.
    """
    if amount_minor is None:
        return "\u2014"
    per = MINOR_PER_MAJOR.get(currency, 100)
    symbol = SYMBOLS.get(currency, f"{currency} ")
    major = int(amount_minor) / per if per > 1 else int(amount_minor)
    return f"{symbol}{major:,.{2 if per > 1 else 0}f}"


def evaluate(
    action: ProposedAction,
    context: RecoveryContext,
    config: PolicyConfig | None = None,
    phase: EvaluationPhase = EvaluationPhase.AUTHORIZATION,
) -> PolicyDecision:
    """Evaluate a proposed action against merchant policy.

    Returns exactly one of AUTO_APPROVED, REQUIRES_APPROVAL, or BLOCKED.
    Never raises on bad input — malformed input is BLOCKED, because a financial
    boundary that throws is a financial boundary that someone will wrap in a
    try/except and accidentally bypass.
    """
    config = config or PolicyConfig()
    now = context.now

    snapshot = policy_hash(config)

    def decide(decision: Decision, rule: str, reason: str, **meta) -> PolicyDecision:
        return PolicyDecision(
            decision=decision,
            rule=rule,
            reason=reason,
            policy_version=config.version,
            policy_hash=snapshot,
            evaluated_at=now,
            phase=phase,
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
        if not isinstance(action.amount_minor, int) or isinstance(
            action.amount_minor, bool
        ):
            return block(
                "invalid_amount",
                "Amount must be an integer number of paise.",
            )
        if action.amount_minor <= 0:
            return block(
                "invalid_amount",
                f"Amount must be positive, got {action.amount_minor} paise.",
                amount_minor=action.amount_minor,
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
        projected = context.recovered_today_minor + action.amount_minor
        if projected > config.max_daily_recovery_minor:
            return block(
                "daily_limit_exceeded",
                f"This action would bring today's recovery total to "
                f"{_rupees(projected)}, above the "
                f"{_rupees(config.max_daily_recovery_minor)} daily cap.",
                projected_minor=projected,
                cap_minor=config.max_daily_recovery_minor,
            )

        if action.amount_minor > config.max_auto_amount_minor:
            return decide(
                Decision.REQUIRES_APPROVAL,
                "above_auto_threshold",
                f"Amount {_rupees(action.amount_minor)} exceeds the "
                f"{_rupees(config.max_auto_amount_minor)} autonomous limit. "
                "Human approval required.",
                amount_minor=action.amount_minor,
                threshold_minor=config.max_auto_amount_minor,
            )

    # --- All checks passed. -------------------------------------------------

    return decide(
        Decision.AUTO_APPROVED,
        "within_policy",
        f"{action.action.value} for {_rupees(action.amount_minor)} is within all "
        "policy limits."
        if is_financial
        else f"{action.action.value} is non-financial and within policy.",
        amount_minor=action.amount_minor,
    )
