"""Outcome Engine.

Converts a verified webhook into a recorded recovery. This is where "we sent a
payment link" becomes "we recovered ₹2,400", and it is the only place that
figure is ever produced.

Rules that make the number trustworthy:

1. **Only Razorpay-originated events count.** A replayed event (source='replay')
   is signed with our own webhook secret, so it proves nothing about whether a
   customer paid. Replays are recorded and linked, but they never contribute to
   recovered revenue. Without this, anyone able to reach the replay endpoint
   could fabricate revenue — which is exactly the P0 that was found and fixed
   in this system.

2. **The amount comes from Razorpay, not from us.** We record what was actually
   paid, not what we hoped would be paid. If they differ, Razorpay is right.

3. **One outcome per execution**, enforced by a unique constraint. Razorpay
   retries webhooks; a retry must not double-count revenue.

4. **The link back to an execution is the reference_id**, which is our
   idempotency key. No fuzzy matching on amount or customer — a guess here
   would attribute revenue to the wrong recovery.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg

log = logging.getLogger(__name__)

#: Events that mean money arrived.
SUCCESS_EVENTS = frozenset({"payment_link.paid", "payment.captured"})
#: Events that close a recovery attempt without money.
FAILURE_EVENTS = frozenset({"payment_link.expired", "payment.failed"})

#: Sources that may contribute to recovered revenue.
#:
#: Trust comes from PROVENANCE, not from the presence of a signature:
#:
#:   "razorpay"      pushed by Razorpay, proven by an HMAC we cannot forge
#:   "razorpay_api"  pulled from Razorpay over an authenticated TLS call,
#:                   which is equally authoritative — it IS Razorpay's answer
#:
#:   "replay"        signed with OUR OWN secret, so it proves nothing about
#:                   whether a customer paid. Never counted.
#:
#: Reconciliation exists because webhooks must never be assumed to arrive.
#: Five real payments were made and confirmed by Razorpay while zero webhooks
#: reached this system (a stale URL in the dashboard). A recovery system whose
#: revenue figure depends on webhook delivery reports zero on a bad day.
TRUSTED_SOURCES = frozenset({"razorpay", "razorpay_api"})

#: Kept for readability at call sites that push webhooks.
TRUSTED_SOURCE = "razorpay"


@dataclass(frozen=True)
class OutcomeResult:
    matched: bool
    execution_id: int | None = None
    recovered_minor: int = 0
    succeeded: bool = False
    counted: bool = False       # False when the event was not Razorpay-originated
    reason: str = ""


async def record_outcome(
    conn: asyncpg.Connection,
    *,
    event_id: str,
    event_type: str,
    reference_id: str | None,
    payment_link_id: str | None,
    amount_minor: int | None,
    source: str,
) -> OutcomeResult:
    """Attribute one verified webhook to a recovery execution, if it belongs to one.

    Returns a result describing what happened. Never raises for an unmatched
    event: most webhooks are ordinary traffic with no recovery behind them.
    """
    if event_type not in SUCCESS_EVENTS | FAILURE_EVENTS:
        return OutcomeResult(matched=False, reason="event type not outcome-bearing")

    execution = await _find_execution(conn, reference_id, payment_link_id)
    if execution is None:
        return OutcomeResult(matched=False,
                             reason="no execution matches this event")

    execution_id = execution["id"]
    succeeded = event_type in SUCCESS_EVENTS

    # Rule 1. Recorded and linked, but never counted.
    if source not in TRUSTED_SOURCES:
        log.warning(
            "outcome.untrusted_source event=%s source=%s execution=%s — "
            "linked but NOT counted as recovered revenue",
            event_id, source, execution_id,
        )
        await _audit(conn, execution_id, event_id,
                     "OUTCOME_IGNORED_UNTRUSTED_SOURCE", 0,
                     f"source={source!r} cannot prove payment")
        return OutcomeResult(matched=True, execution_id=execution_id,
                             succeeded=succeeded, counted=False,
                             reason=f"source {source!r} is not Razorpay-originated")

    # Rule 2. Razorpay's amount wins.
    recovered = int(amount_minor or 0) if succeeded else 0

    # Rule 3. One outcome per execution; a webhook retry must not double-count.
    inserted = await conn.fetchval(
        """
        INSERT INTO recovery_outcomes
            (execution_id, recovered_minor, succeeded, verified_by_event)
        VALUES ($1,$2,$3,$4)
        ON CONFLICT (execution_id) DO NOTHING
        RETURNING id
        """,
        execution_id, recovered, succeeded, event_id,
    )
    if inserted is None:
        log.info("outcome.duplicate execution=%s event=%s", execution_id, event_id)
        return OutcomeResult(matched=True, execution_id=execution_id,
                             succeeded=succeeded, counted=False,
                             reason="outcome already recorded for this execution")

    await conn.execute(
        """
        UPDATE payments SET status = CASE WHEN $2 THEN 'captured'::payment_status
                                          ELSE status END,
                            updated_at = now()
         WHERE id = (SELECT ra.payment_id FROM recovery_actions ra
                      JOIN execution_records er ON er.action_id = ra.id
                     WHERE er.id = $1)
        """,
        execution_id, succeeded,
    )
    await conn.execute(
        """
        UPDATE revenue_incidents SET status='resolved', resolved_at=now()
         WHERE id = (SELECT ra.incident_id FROM recovery_actions ra
                      JOIN execution_records er ON er.action_id = ra.id
                     WHERE er.id = $1)
           AND status = 'recovering'
           AND NOT EXISTS (
                 SELECT 1 FROM recovery_actions ra2
                  WHERE ra2.incident_id = (SELECT ra3.incident_id
                                             FROM recovery_actions ra3
                                             JOIN execution_records er3
                                               ON er3.action_id = ra3.id
                                            WHERE er3.id = $1)
                    AND ra2.status IN ('approved','awaiting_approval','executing'))
        """,
        execution_id,
    )
    await _audit(
        conn, execution_id, event_id,
        "REVENUE_RECOVERED" if succeeded else "RECOVERY_FAILED",
        recovered,
        # Prose, not an event name: `event_type` already carries the
        # machine-readable value, and this string is shown to a merchant.
        "Confirmed paid by Razorpay" if succeeded
        else "Recovery did not convert",
    )
    log.info("outcome.recorded execution=%s recovered=%s succeeded=%s",
             execution_id, recovered, succeeded)

    return OutcomeResult(matched=True, execution_id=execution_id,
                         recovered_minor=recovered, succeeded=succeeded,
                         counted=True, reason="recorded")


async def _find_execution(conn, reference_id: str | None,
                          payment_link_id: str | None):
    """Match strictly. Rule 4: no fuzzy matching on amount or customer."""
    if reference_id:
        row = await conn.fetchrow(
            "SELECT id FROM execution_records WHERE idempotency_key=$1",
            reference_id)
        if row:
            return row
    if payment_link_id:
        return await conn.fetchrow(
            "SELECT id FROM execution_records WHERE razorpay_ref=$1",
            payment_link_id)
    return None


async def _audit(conn, execution_id: int, event_id: str, event_type: str,
                 amount_minor: int, reason: str) -> None:
    await conn.execute(
        """
        INSERT INTO audit_events
            (actor, event_type, merchant_id, customer_id, payment_id,
             incident_id, action_id, execution_id, amount_minor, reason, metadata)
        SELECT 'OUTCOME_ENGINE', $2, p.merchant_id, ra.customer_id, ra.payment_id,
               ra.incident_id, ra.id, er.id, $3, $4,
               jsonb_build_object('verified_by_event', $5::text)
          FROM execution_records er
          JOIN recovery_actions ra ON ra.id = er.action_id
          JOIN payments p ON p.id = ra.payment_id
         WHERE er.id = $1
        """,
        execution_id, event_type, amount_minor, reason, event_id,
    )
