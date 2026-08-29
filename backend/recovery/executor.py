"""Action Executor.

The only component that can move money, and the last gate before it does.

It does NOT re-decide anything. It verifies that a decision was already made
correctly, and refuses otherwise. Every refusal is explicit and audited.

Seven preconditions, all of which must hold:

1. The action exists and is in an executable state.
2. A policy decision exists for it. No decision means no execution — an action
   without a recorded ruling is unexplainable, and unexplainable financial
   actions are the thing this system exists to prevent.
3. The decision is AUTO_APPROVED, or REQUIRES_APPROVAL **with** a recorded
   human approval.
4. The action has not expired.
5. The amount still matches what policy ruled on. A changed amount invalidates
   the ruling — this is the post-approval tamper check.
6. The payment is still in a recoverable state. Between planning and execution
   the customer may have paid by other means; charging them again is the
   already-paid failure.
7. No prior successful execution exists. Enforced by a database unique
   constraint on the idempotency key, not by checking first and hoping.

A Razorpay timeout is NOT treated as a failure. The action may have succeeded
on their side, so the execution is left `pending` for the webhook to resolve.
Retrying blindly is how a timeout becomes a double charge.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import asyncpg

from ..config import Settings
from ..integrations.razorpay_client import PaymentLink, RazorpayClient, RazorpayError
from ..policy import (
    ActionType,
    Decision,
    EvaluationPhase,
    PaymentStatus,
    PolicyConfig,
    ProposedAction,
    RecoveryContext,
    evaluate,
    load_merchant_config,
    merchant_day_start,
)

log = logging.getLogger(__name__)

EXECUTABLE_STATUSES = {"approved"}

# NOTE: there is deliberately no RECOVERABLE_PAYMENT_STATUSES here any more.
# Which payment states are recoverable is a POLICY question, and the Policy
# Engine answers it. A copy in the executor would be a second definition to
# keep in sync — and that duplication is precisely why the daily-cap check went
# missing from this file in the first place.

#: Execution states that represent money already committed today. `pending`
#: counts: a timed-out execution may well have created a payment link on
#: Razorpay's side, so treating it as free budget would let the daily cap be
#: overshot by exactly the actions we are least sure about.
COMMITTED_EXECUTION_STATUSES = ("succeeded", "pending")


async def daily_committed_paise(
    conn: asyncpg.Connection, now: datetime, timezone_name: str
) -> int:
    """Money committed to recovery so far today.

    Single source of truth, shared by planning and execution. Two different
    definitions of "spent today" is how a cap gets breached while both sides
    believe they are within it.

    CLOCK CONSISTENCY: the window boundary comes from the caller's `now`, and
    `execution_records.created_at` is written from that same `now` rather than
    the database default. Mixing an application clock with a database default
    means the boundary and the rows being compared against it come from two
    different sources, which can disagree under container clock skew, a
    replica, or a timezone misconfiguration. One clock decides the window and
    stamps the rows, so the daily total is always computed over the period it
    claims to.

    TIMEZONE: the day boundary is the MERCHANT's, not UTC. The boundary is
    computed in Python by `merchant_day_start` so the same definition of
    "today" is used everywhere, rather than being re-derived in SQL where it
    could drift out of step. See docs/DECISIONS.md D14.
    """
    day_start = merchant_day_start(now, timezone_name)
    total = await conn.fetchval(
        """
        SELECT coalesce(sum(amount_paise), 0)
          FROM execution_records
         WHERE status::text = ANY($2::text[])
           AND created_at >= $1
        """,
        day_start, list(COMMITTED_EXECUTION_STATUSES),
    )
    return int(total or 0)


class ExecutionRefused(Exception):
    """A precondition failed. No money moved."""

    def __init__(self, rule: str, message: str):
        super().__init__(message)
        self.rule = rule


@dataclass(frozen=True)
class ExecutionResult:
    action_id: int
    execution_id: int
    status: str                 # succeeded | pending | failed
    idempotency_key: str
    razorpay_ref: str | None = None
    short_url: str | None = None
    error: str | None = None
    reused: bool = False        # returned an existing execution, did not re-execute


#: Razorpay rejects a reference_id longer than 40 characters:
#:   "reference_id: the length must be no more than 40."
#: The key is sent AS the reference_id so Razorpay enforces duplicate
#: suppression too, so it has to fit. 3-char prefix + 32 hex = 35.
IDEMPOTENCY_KEY_HEX = 32
IDEMPOTENCY_KEY_PREFIX = "rv_"
RAZORPAY_REFERENCE_ID_MAX = 40


def idempotency_key(action_id: int, amount_paise: int, policy_version: str) -> str:
    """Stable per (action, amount, policy version).

    Amount is included deliberately: if the amount changed, this is not the
    same financial action and must not silently reuse the old execution.

    128 bits of SHA-256 is far more than enough to avoid collisions at any
    volume this system will ever see.
    """
    raw = f"revenant:v1:{action_id}:{amount_paise}:{policy_version}"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:IDEMPOTENCY_KEY_HEX]
    key = f"{IDEMPOTENCY_KEY_PREFIX}{digest}"
    if len(key) > RAZORPAY_REFERENCE_ID_MAX:
        # NOT an assert: `python -O` strips asserts, and a financial guard that
        # disappears under an optimisation flag is not a guard.
        raise ValueError(
            f"Idempotency key is {len(key)} chars, over Razorpay's "
            f"{RAZORPAY_REFERENCE_ID_MAX} limit for reference_id."
        )
    return key


async def execute_action(
    conn: asyncpg.Connection,
    client: RazorpayClient,
    *,
    action_id: int,
    merchant_id: str,
    settings: Settings,
    now: datetime | None = None,
    policy_config: PolicyConfig | None = None,
) -> ExecutionResult:
    """Execute one approved recovery action. Raises ExecutionRefused if any
    precondition fails."""
    now = now or datetime.now(timezone.utc)

    row = await conn.fetchrow(
        """
        SELECT ra.id, ra.payment_id, ra.customer_id, ra.action::text AS action,
               ra.amount_paise, ra.status::text AS status, ra.expires_at,
               ra.proposed_at,
               p.status::text AS payment_status, p.merchant_id,
               c.email, c.phone, c.opted_out,
               pd.result::text AS authorized_result,
               pd.policy_version AS authorized_policy_version,
               pd.policy_hash    AS authorized_policy_hash,
               pd.evaluated_at   AS authorized_at,
               pd.metadata AS policy_metadata,
               ap.approved AS approval_granted, ap.id AS approval_id,
               (SELECT count(*) FROM execution_records er2
                 WHERE er2.action_id = ra.id) AS prior_attempts
          FROM recovery_actions ra
          JOIN payments  p ON p.id = ra.payment_id
          JOIN customers c ON c.id = ra.customer_id
          LEFT JOIN policy_decisions pd
                 ON pd.action_id = ra.id AND pd.phase = 'authorization'
          LEFT JOIN approvals ap ON ap.action_id = ra.id
         WHERE ra.id = $1
         LIMIT 1
        """,
        action_id,
    )

    if row is None:
        raise ExecutionRefused("action_not_found", f"No action {action_id}.")
    if row["merchant_id"] != merchant_id:
        raise ExecutionRefused("wrong_merchant", "Action belongs to another merchant.")

    # --- 2. a policy decision must exist -----------------------------------
    if row["authorized_result"] is None:
        raise ExecutionRefused(
            "no_policy_decision",
            f"Action {action_id} has no policy decision. Refusing to execute.",
        )

    result = row["authorized_result"]

    # --- 3. blocked, or approval required and not granted ------------------
    if result == Decision.BLOCKED.value:
        raise ExecutionRefused(
            "policy_blocked",
            f"Policy BLOCKED action {action_id}. Refusing to execute.",
        )
    if result == Decision.REQUIRES_APPROVAL.value:
        if row["approval_granted"] is None:
            raise ExecutionRefused(
                "approval_missing",
                f"Action {action_id} requires human approval, none recorded.",
            )
        if row["approval_granted"] is False:
            raise ExecutionRefused(
                "approval_denied",
                f"Action {action_id} was explicitly denied by a human.",
            )

    # --- 7. idempotency -- checked EARLY, and deliberately so ---------------
    #
    # If an execution already exists for this key, return it and stop. This
    # runs before the state, expiry, and amount checks because those describe
    # whether a NEW execution may start, not what happened to one that already
    # did.
    #
    # Ordering this later was a real bug: a Razorpay timeout leaves the action
    # 'executing' and the execution 'pending', so a retry hit the state check
    # and was refused as "not_executable" — reporting a refusal for an action
    # that may well have succeeded on Razorpay's side. Returning the existing
    # pending record is both truthful and safe.
    #
    # Reaching this point means a policy decision exists, so any execution
    # found here already passed every check when it was created.
    key = idempotency_key(action_id, int(row["amount_paise"]),
                          row["authorized_policy_version"])
    existing = await conn.fetchrow(
        """
        SELECT id, status::text AS status, razorpay_ref, razorpay_short_url, error
          FROM execution_records WHERE idempotency_key = $1
        """,
        key,
    )
    if existing is not None:
        log.info("executor.idempotent_hit action=%s status=%s key=%s",
                 action_id, existing["status"], key[:12])
        return ExecutionResult(
            action_id=action_id, execution_id=existing["id"],
            status=existing["status"], idempotency_key=key,
            razorpay_ref=existing["razorpay_ref"],
            short_url=existing["razorpay_short_url"],
            error=existing["error"], reused=True,
        )

    # --- 1. executable state ------------------------------------------------
    if row["status"] not in EXECUTABLE_STATUSES:
        raise ExecutionRefused(
            "not_executable",
            f"Action {action_id} is {row['status']!r}, not approved.",
        )

    # --- 4. expiry ------------------------------------------------------------
    # Deliberately NOT checked here. `action_expired` is a Policy Engine rule,
    # and the engine evaluates it below from the action's real proposed_at.
    #
    # An earlier version checked expiry here and passed proposed_at=now to the
    # engine, which both duplicated the rule AND suppressed the engine's own
    # copy of it. The refusal then happened before the execution-phase
    # evaluation was recorded, so "why wasn't it executed?" had no answer in the
    # audit trail — breaking D15 for every expired action.

    # --- 5. amount unchanged since the ruling -------------------------------
    meta = row["policy_metadata"]
    if isinstance(meta, str):
        meta = json.loads(meta)
    ruled_amount = (meta or {}).get("amount_paise")
    if ruled_amount is not None and int(ruled_amount) != int(row["amount_paise"]):
        raise ExecutionRefused(
            "amount_changed",
            f"Amount changed after policy ruling "
            f"({ruled_amount} -> {row['amount_paise']} paise). Refusing.",
        )

    # --- 6. RE-EVALUATE POLICY AGAINST CURRENT STATE ------------------------
    #
    # The stored decision records that this action was AUTHORISED. It does not
    # prove it is still PERMITTED: the daily cap, the payment's status, the
    # customer's opt-out and the retry count all move between planning and
    # execution.
    #
    # Trusting the stored ruling alone allowed a real breach — 22 actions
    # approved across a day totalled Rs 38,893 against a Rs 25,000 cap and all
    # 22 executed, because nothing re-checked. The Policy Engine has to be the
    # gate at the moment money moves, not only when the plan was drawn up.
    #
    # CONCURRENCY: the cap check and the claim that consumes budget must be
    # atomic. Without a lock, two executors can both read the daily total
    # before either writes, both conclude there is headroom, and together
    # exceed the cap. The window is narrow — the claim is written before the
    # Razorpay call, and `pending` counts toward the total — but "narrow" is
    # not a property to rely on for a financial limit.
    #
    # A transaction-scoped advisory lock keyed on the merchant serialises cap
    # evaluation PER MERCHANT: two merchants never block each other, and two
    # executors for the same merchant cannot interleave read-and-claim.
    # Released automatically when the transaction ends, including on error.
    #
    # Same deterministic engine, same rules, current facts.
    #
    # The merchant's own limits, timezone and currency. An explicit override is
    # honoured (tests, simulator); the default is always what THIS merchant is
    # configured with, never a global constant.
    if policy_config is None:
        policy_config = (await load_merchant_config(conn, merchant_id)).policy

    # ONE transaction: acquire the per-merchant lock, evaluate policy against
    # the current daily total, and claim the budget. Committing before the
    # Razorpay call is deliberate — a network call must never be made while
    # holding a lock, and the claim must be durable before money can move.
    refusal: ExecutionRefused | None = None
    execution_id = None
    race_lost = False

    async with conn.transaction():
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext($1))",
            f"revenant:cap:{merchant_id}",
        )

        fresh_context = RecoveryContext(
            payment_status=PaymentStatus(row["payment_status"]),
            customer_opted_out=row["opted_out"],
            prior_attempts=int(row["prior_attempts"] or 0),
            last_attempt_at=None,   # this action IS the current attempt
            recovered_today_paise=await daily_committed_paise(
                conn, now, policy_config.business_timezone),
            now=now,
        )
        fresh_action = ProposedAction(
            action=ActionType(row["action"]),
            customer_id=row["customer_id"],
            payment_id=row["payment_id"],
            amount_paise=int(row["amount_paise"]),
            # The action's real proposal time, so the engine's own
            # `action_expired` rule evaluates correctly (D13: one source of
            # truth for every policy rule).
            proposed_at=row["proposed_at"],
        )
        fresh = evaluate(fresh_action, fresh_context, policy_config,
                         phase=EvaluationPhase.EXECUTION)

        # Recorded whether it permits or refuses. A refusal leaves no execution
        # record, so without this row the reason money did NOT move would exist
        # only in a log line (D15).
        await _record_execution_decision(conn, action_id, fresh)

        if fresh.decision is Decision.BLOCKED:
            await _mark(conn, action_id,
                        "failed" if fresh.rule != "action_expired" else "expired")
            await _audit(conn, "POLICY_ENGINE", "EXECUTION_BLOCKED_AT_RUNTIME",
                         row, action_id, None, reason=fresh.reason)
            refusal = ExecutionRefused(fresh.rule, fresh.reason)

        # An action that policy now says needs approval must not slip through
        # on a stale AUTO_APPROVED ruling.
        elif (fresh.decision is Decision.REQUIRES_APPROVAL
                and row["approval_granted"] is not True):
            await _audit(conn, "POLICY_ENGINE", "EXECUTION_BLOCKED_AT_RUNTIME",
                         row, action_id, None, reason=fresh.reason)
            refusal = ExecutionRefused("approval_required_now", fresh.reason)

        else:
            # Claim the key while still holding the lock, so the budget this
            # action consumes is visible to every other executor before any of
            # them re-reads the daily total.
            try:
                execution_id = await conn.fetchval(
                    """
                    INSERT INTO execution_records
                        (action_id, idempotency_key, status, amount_paise,
                         attempts, created_at, execution_policy_version,
                         execution_policy_hash, execution_policy_evaluated_at)
                    VALUES ($1,$2,'pending',$3,1,$4,$5,$6,$7)
                    RETURNING id
                    """,
                    action_id, key, int(row["amount_paise"]), now,
                    fresh.policy_version, fresh.policy_hash, fresh.evaluated_at,
                )
            except asyncpg.UniqueViolationError:
                race_lost = True

    # Raised outside the transaction so the audit row above is committed: a
    # refusal must remain explainable afterwards.
    if refusal is not None:
        raise refusal

    if race_lost:
        # Another worker claimed this key between our idempotency lookup and
        # this insert. The database prevented the double charge; the loser of
        # the race should receive the winner's result, not a 500.
        winner = await conn.fetchrow(
            """
            SELECT id, status::text AS status, razorpay_ref, razorpay_short_url,
                   error
              FROM execution_records WHERE idempotency_key = $1
            """,
            key,
        )
        log.info("executor.race_lost action=%s key=%s", action_id, key[:12])
        return ExecutionResult(
            action_id=action_id, execution_id=winner["id"],
            status=winner["status"], idempotency_key=key,
            razorpay_ref=winner["razorpay_ref"],
            short_url=winner["razorpay_short_url"],
            error=winner["error"], reused=True,
        )
    await _mark(conn, action_id, "executing")
    await _audit(conn, "EXECUTOR", "EXECUTION_STARTED", row, action_id,
                 execution_id, reason=f"idempotency_key={key[:12]}…")

    if row["action"] != ActionType.CREATE_PAYMENT_LINK.value:
        # Non-financial action: nothing to call, record success.
        await _complete(conn, execution_id, action_id, "succeeded")
        return ExecutionResult(action_id, execution_id, "succeeded", key)

    try:
        link: PaymentLink = await client.create_payment_link(
            amount_paise=int(row["amount_paise"]),
            reference_id=key,          # Razorpay's own duplicate guard
            description=f"Recovery for payment {row['payment_id']}",
            customer_email=row["email"],
            customer_contact=row["phone"],
            # The merchant's configured currency, enforced by the client. A
            # mismatch is refused rather than silently settled in INR.
            currency=policy_config.currency,
        )
    except RazorpayError as e:
        if e.retryable:
            # Timeout or 5xx: the action MAY have succeeded on Razorpay's side.
            # Leave it pending for the webhook to resolve. Retrying now is how
            # a timeout becomes a double charge.
            await conn.execute(
                "UPDATE execution_records SET error=$2 WHERE id=$1",
                execution_id, f"pending after retryable error: {e}",
            )
            await _audit(conn, "EXECUTOR", "EXECUTION_PENDING", row, action_id,
                         execution_id, error=str(e))
            log.warning("executor.pending action=%s err=%s", action_id, e)
            return ExecutionResult(action_id, execution_id, "pending", key,
                                   error=str(e))

        await _complete(conn, execution_id, action_id, "failed", error=str(e))
        await _audit(conn, "EXECUTOR", "EXECUTION_FAILED", row, action_id,
                     execution_id, error=str(e))
        log.error("executor.failed action=%s err=%s", action_id, e)
        return ExecutionResult(action_id, execution_id, "failed", key, error=str(e))

    await conn.execute(
        """
        UPDATE execution_records
           SET status='succeeded', razorpay_ref=$2, razorpay_short_url=$3,
               executed_at=now()
         WHERE id=$1
        """,
        execution_id, link.id, link.short_url,
    )
    await _mark(conn, action_id, "executed")
    await _audit(conn, "EXECUTOR", "PAYMENT_LINK_CREATED", row, action_id,
                 execution_id, reason=f"razorpay_ref={link.id}")
    log.info("executor.succeeded action=%s ref=%s", action_id, link.id)

    return ExecutionResult(action_id, execution_id, "succeeded", key,
                           razorpay_ref=link.id, short_url=link.short_url)


async def _mark(conn, action_id: int, status: str) -> None:
    await conn.execute(
        "UPDATE recovery_actions SET status=$2::action_status WHERE id=$1",
        action_id, status,
    )


async def _complete(conn, execution_id: int, action_id: int, status: str,
                    error: str | None = None) -> None:
    await conn.execute(
        """
        UPDATE execution_records
           SET status=$2::execution_status, error=$3, executed_at=now()
         WHERE id=$1
        """,
        execution_id, status, error,
    )
    await _mark(conn, action_id, "executed" if status == "succeeded" else "failed")


async def _audit(conn, actor: str, event_type: str, row, action_id: int,
                 execution_id: int | None, reason: str | None = None,
                 error: str | None = None) -> None:
    await conn.execute(
        """
        INSERT INTO audit_events
            (actor, event_type, merchant_id, customer_id, payment_id,
             action_id, execution_id, amount_paise, policy_version,
             policy_result, reason, error)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::policy_result,$11,$12)
        """,
        actor, event_type, row["merchant_id"], row["customer_id"],
        row["payment_id"], action_id, execution_id, row["amount_paise"],
        row["authorized_policy_version"], row["authorized_result"], reason, error,
    )


async def _record_execution_decision(conn, action_id: int, decision) -> None:
    """Append the execution-phase evaluation.

    Never overwrites the authorization row. The two are separate facts: one
    says why the action was allowed to be planned, the other why money did or
    did not move. Collapsing them into a single mutable field would destroy the
    ability to answer either question afterwards.
    """
    await conn.execute(
        """
        INSERT INTO policy_decisions
            (action_id, phase, result, rule, reason, policy_version,
             policy_hash, metadata, evaluated_at)
        VALUES ($1,'execution',$2::policy_result,$3,$4,$5,$6,$7,$8)
        """,
        action_id, decision.decision.value, decision.rule, decision.reason,
        decision.policy_version, decision.policy_hash,
        json.dumps(decision.metadata or {}), decision.evaluated_at,
    )


async def resolve_pending_execution(
    conn: asyncpg.Connection,
    client: RazorpayClient,
    *,
    execution_id: int,
) -> ExecutionResult:
    """Resolve one execution left `pending` by a timeout or a rate limit.

    A pending execution is the genuinely ambiguous case: we claimed the budget
    and called Razorpay, and we do not know whether the call landed. Both
    possible answers are dangerous if guessed — marking it failed risks a
    duplicate charge on retry, marking it succeeded fabricates revenue.

    So we ask Razorpay, using the SAME reference_id. Two outcomes, both safe:

    * Razorpay rejects it as a duplicate reference_id -> the link already
      exists, so the original call DID land. Fetch and record it.
    * Razorpay creates it -> the original call did not land, and this is the
      first and only link.

    Either way exactly one payment link exists per execution, because the
    reference_id is the idempotency key and Razorpay enforces uniqueness on it.
    """
    row = await conn.fetchrow(
        """
        SELECT er.id, er.action_id, er.idempotency_key, er.amount_paise,
               er.status::text AS status, ra.payment_id, ra.customer_id,
               c.email, c.phone, p.merchant_id
          FROM execution_records er
          JOIN recovery_actions ra ON ra.id = er.action_id
          JOIN customers c ON c.id = ra.customer_id
          JOIN payments p ON p.id = ra.payment_id
         WHERE er.id = $1
        """,
        execution_id,
    )
    if row is None:
        raise ExecutionRefused("execution_not_found", f"No execution {execution_id}.")
    if row["status"] != "pending":
        return ExecutionResult(
            action_id=row["action_id"], execution_id=execution_id,
            status=row["status"], idempotency_key=row["idempotency_key"],
            reused=True,
        )

    key = row["idempotency_key"]
    try:
        link = await client.create_payment_link(
            amount_paise=int(row["amount_paise"]),
            reference_id=key,
            description=f"Recovery for payment {row['payment_id']}",
            customer_email=row["email"],
            customer_contact=row["phone"],
        )
        ref, short_url = link.id, link.short_url
    except RazorpayError as e:
        if "already exists" in str(e):
            # The original call DID land. The link exists; find it rather than
            # creating a second one.
            found = await client.find_payment_link_by_reference(key)
            if found is None:
                # Razorpay says it exists but will not show it. Leave pending:
                # inventing an outcome here would either fabricate revenue or
                # invite a duplicate charge.
                log.warning("executor.pending_unresolved execution=%s", execution_id)
                return ExecutionResult(
                    action_id=row["action_id"], execution_id=execution_id,
                    status="pending", idempotency_key=key,
                    error="duplicate reported but link not retrievable",
                )
            ref, short_url = found["id"], found["short_url"]
        elif e.retryable:
            return ExecutionResult(
                action_id=row["action_id"], execution_id=execution_id,
                status="pending", idempotency_key=key, error=str(e))
        else:
            await _complete(conn, execution_id, row["action_id"], "failed",
                            error=str(e))
            return ExecutionResult(
                action_id=row["action_id"], execution_id=execution_id,
                status="failed", idempotency_key=key, error=str(e))

    await conn.execute(
        """
        UPDATE execution_records
           SET status='succeeded', razorpay_ref=$2, razorpay_short_url=$3,
               executed_at=now(), attempts=attempts+1
         WHERE id=$1
        """,
        execution_id, ref, short_url,
    )
    await _mark(conn, row["action_id"], "executed")
    await conn.execute(
        """
        INSERT INTO audit_events
            (actor, event_type, merchant_id, customer_id, payment_id,
             action_id, execution_id, amount_paise, reason)
        VALUES ('EXECUTOR','PENDING_RESOLVED',$1,$2,$3,$4,$5,$6,$7)
        """,
        row["merchant_id"], row["customer_id"], row["payment_id"],
        row["action_id"], execution_id, int(row["amount_paise"]),
        f"razorpay_ref={ref}",
    )
    log.info("executor.pending_resolved execution=%s ref=%s", execution_id, ref)
    return ExecutionResult(
        action_id=row["action_id"], execution_id=execution_id,
        status="succeeded", idempotency_key=key, razorpay_ref=ref,
        short_url=short_url,
    )
