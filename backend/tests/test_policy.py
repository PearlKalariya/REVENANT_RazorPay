"""Policy Engine tests.

The Policy Engine is the financial safety boundary, so these tests are
deliberately exhaustive. They cover the seven mandated failure scenarios,
rule precedence, and fail-closed behaviour on malformed input.

`now` is injected everywhere — no test depends on the wall clock.
"""

from datetime import datetime, timedelta

import pytest

from backend.policy import (
    ActionType,
    Decision,
    PaymentStatus,
    PolicyConfig,
    ProposedAction,
    RecoveryContext,
    evaluate,
)

NOW = datetime(2026, 8, 27, 12, 0, 0)
RUPEE = 100  # paise


def action(amount_rupees=2_400, action_type=ActionType.CREATE_PAYMENT_LINK, **kw):
    defaults = dict(
        action=action_type,
        customer_id="CUST_829",
        payment_id="pay_TEST123",
        amount_minor=amount_rupees * RUPEE,
        proposed_at=NOW,
    )
    defaults.update(kw)
    return ProposedAction(**defaults)


def context(**kw):
    defaults = dict(
        payment_status=PaymentStatus.FAILED,
        customer_opted_out=False,
        prior_attempts=0,
        recovered_today_minor=0,
        now=NOW,
        last_attempt_at=None,
    )
    defaults.update(kw)
    return RecoveryContext(**defaults)


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_within_all_limits_is_auto_approved():
    d = evaluate(action(2_400), context())
    assert d.decision is Decision.AUTO_APPROVED
    assert d.rule == "within_policy"
    assert d.is_executable


def test_decision_stamps_policy_version():
    d = evaluate(action(), context())
    assert d.policy_version == "v1"
    assert d.evaluated_at == NOW


def test_amount_exactly_at_threshold_is_auto_approved():
    """Boundary: the limit is inclusive. ₹5,000 passes, ₹5,000.01 does not."""
    d = evaluate(action(5_000), context())
    assert d.decision is Decision.AUTO_APPROVED


# --------------------------------------------------------------------------
# Mandated failure scenario 1 — already paid
# --------------------------------------------------------------------------


def test_already_paid_is_blocked():
    d = evaluate(action(), context(payment_status=PaymentStatus.CAPTURED))
    assert d.decision is Decision.BLOCKED
    assert d.rule == "already_paid"
    assert "double-charge" in d.reason


@pytest.mark.parametrize(
    "status",
    [PaymentStatus.CREATED, PaymentStatus.AUTHORIZED, PaymentStatus.REFUNDED],
)
def test_non_recoverable_states_are_blocked(status):
    d = evaluate(action(), context(payment_status=status))
    assert d.decision is Decision.BLOCKED
    assert d.rule == "payment_not_recoverable"


# --------------------------------------------------------------------------
# Mandated failure scenario 2 — amount too high
# --------------------------------------------------------------------------


def test_amount_above_threshold_requires_approval():
    d = evaluate(action(7_800), context())
    assert d.decision is Decision.REQUIRES_APPROVAL
    assert d.rule == "above_auto_threshold"
    assert not d.is_executable


def test_high_amount_is_not_auto_executable():
    """The demo claim: ₹7,800 must never reach the executor unattended."""
    d = evaluate(action(7_800), context())
    assert not d.is_executable


# --------------------------------------------------------------------------
# Mandated failure scenario 3 — daily limit
# --------------------------------------------------------------------------


def test_daily_limit_exceeded_is_blocked():
    d = evaluate(action(3_000), context(recovered_today_minor=2_400_000))
    assert d.decision is Decision.BLOCKED
    assert d.rule == "daily_limit_exceeded"


def test_daily_limit_exactly_at_cap_is_allowed():
    """₹24,000 recovered + ₹1,000 = ₹25,000 cap exactly. Inclusive."""
    d = evaluate(action(1_000), context(recovered_today_minor=2_400_000))
    assert d.decision is Decision.AUTO_APPROVED


def test_daily_limit_blocks_before_approval_escalation():
    """A ₹10,000 action over the daily cap must be BLOCKED, not escalated.

    Precedence matters: escalating would put a cap breach in front of a human
    who could approve it, silently defeating the cap.
    """
    d = evaluate(action(10_000), context(recovered_today_minor=2_400_000))
    assert d.decision is Decision.BLOCKED
    assert d.rule == "daily_limit_exceeded"


# --------------------------------------------------------------------------
# Mandated failure scenario 6 — customer opt-out
# --------------------------------------------------------------------------


def test_opted_out_customer_is_blocked():
    d = evaluate(action(), context(customer_opted_out=True))
    assert d.decision is Decision.BLOCKED
    assert d.rule == "customer_opted_out"


def test_opt_out_beats_everything_else():
    """Opt-out is checked before amount, so it cannot be escalated past."""
    d = evaluate(action(50_000), context(customer_opted_out=True))
    assert d.decision is Decision.BLOCKED
    assert d.rule == "customer_opted_out"


# --------------------------------------------------------------------------
# Mandated failure scenario 7 — agent proposes an unsafe action
# --------------------------------------------------------------------------


def test_agent_proposing_huge_amount_cannot_auto_execute():
    """Policy overrides the agent. Always."""
    d = evaluate(action(5_000_000), context())
    assert not d.is_executable


def test_unknown_action_type_is_blocked():
    """A hallucinated action name fails closed."""
    bad = action()
    object.__setattr__(bad, "action", "REFUND_EVERYTHING")
    d = evaluate(bad, context())
    assert d.decision is Decision.BLOCKED
    assert d.rule == "unknown_action"


@pytest.mark.parametrize("amount", [0, -1, -500_000])
def test_invalid_amounts_are_blocked(amount):
    d = evaluate(action(amount_minor=amount), context())
    assert d.decision is Decision.BLOCKED
    assert d.rule == "invalid_amount"


def test_non_integer_amount_is_blocked():
    d = evaluate(action(amount_minor=2400.5), context())
    assert d.decision is Decision.BLOCKED
    assert d.rule == "invalid_amount"


def test_boolean_amount_is_blocked():
    """bool is a subclass of int in Python. True must not read as ₹0.01."""
    d = evaluate(action(amount_minor=True), context())
    assert d.decision is Decision.BLOCKED
    assert d.rule == "invalid_amount"


def test_missing_identifiers_are_blocked():
    d = evaluate(action(customer_id=""), context())
    assert d.decision is Decision.BLOCKED
    assert d.rule == "missing_identifiers"


# --------------------------------------------------------------------------
# Retry limits and cooldown (stopping rules)
# --------------------------------------------------------------------------


def test_retry_limit_exceeded_is_blocked():
    d = evaluate(action(), context(prior_attempts=2))
    assert d.decision is Decision.BLOCKED
    assert d.rule == "retry_limit_exceeded"


def test_one_prior_attempt_still_allowed():
    d = evaluate(action(), context(prior_attempts=1))
    assert d.decision is Decision.AUTO_APPROVED


def test_cooldown_active_is_blocked():
    d = evaluate(
        action(),
        context(prior_attempts=1, last_attempt_at=NOW - timedelta(minutes=10)),
    )
    assert d.decision is Decision.BLOCKED
    assert d.rule == "cooldown_active"
    assert d.metadata["minutes_remaining"] > 0


def test_cooldown_elapsed_is_allowed():
    d = evaluate(
        action(),
        context(prior_attempts=1, last_attempt_at=NOW - timedelta(minutes=31)),
    )
    assert d.decision is Decision.AUTO_APPROVED


# --------------------------------------------------------------------------
# Action expiry
# --------------------------------------------------------------------------


def test_stale_action_is_blocked():
    d = evaluate(action(proposed_at=NOW - timedelta(hours=3)), context())
    assert d.decision is Decision.BLOCKED
    assert d.rule == "action_expired"


def test_future_dated_action_is_blocked():
    d = evaluate(action(proposed_at=NOW + timedelta(minutes=5)), context())
    assert d.decision is Decision.BLOCKED
    assert d.rule == "action_from_future"


# --------------------------------------------------------------------------
# Non-financial actions
# --------------------------------------------------------------------------


def test_notification_skips_amount_cap():
    """A notification moves no money, so the ₹5,000 cap does not apply."""
    d = evaluate(
        action(99_999, action_type=ActionType.SEND_RECOVERY_NOTIFICATION),
        context(),
    )
    assert d.decision is Decision.AUTO_APPROVED


def test_notification_still_respects_opt_out():
    """But contacting an opted-out customer is exactly what opt-out forbids."""
    d = evaluate(
        action(0, action_type=ActionType.SEND_RECOVERY_NOTIFICATION),
        context(customer_opted_out=True),
    )
    assert d.decision is Decision.BLOCKED
    assert d.rule == "customer_opted_out"


def test_notification_still_respects_already_paid():
    d = evaluate(
        action(0, action_type=ActionType.SEND_RECOVERY_NOTIFICATION),
        context(payment_status=PaymentStatus.CAPTURED),
    )
    assert d.decision is Decision.BLOCKED


# --------------------------------------------------------------------------
# Configurability and determinism
# --------------------------------------------------------------------------


def test_custom_config_changes_threshold():
    strict = PolicyConfig(max_auto_amount_minor=100_000)  # ₹1,000
    d = evaluate(action(2_400), context(), strict)
    assert d.decision is Decision.REQUIRES_APPROVAL


def test_evaluation_is_deterministic():
    a, c = action(7_800), context()
    results = {evaluate(a, c).decision for _ in range(50)}
    assert results == {Decision.REQUIRES_APPROVAL}


def test_every_decision_carries_a_rule_and_reason():
    """Audit requirement: every decision must explain itself."""
    cases = [
        (action(), context()),
        (action(7_800), context()),
        (action(), context(customer_opted_out=True)),
        (action(), context(payment_status=PaymentStatus.CAPTURED)),
        (action(), context(prior_attempts=5)),
    ]
    for a, c in cases:
        d = evaluate(a, c)
        assert d.rule, "decision must name the rule that fired"
        assert d.reason, "decision must carry a human-readable reason"
        assert d.policy_version
