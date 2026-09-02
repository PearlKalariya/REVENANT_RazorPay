"""Outcome reconciliation.

Webhooks must never be assumed to arrive — your spec says so, and it was
demonstrated the hard way: five real test payments completed and were confirmed
by Razorpay while ZERO webhooks reached this system, because the dashboard held
a stale tunnel URL. A recovery product whose headline number depends on webhook
delivery reports zero revenue on a bad network day.

So the system also PULLS. For every execution without a recorded outcome, ask
Razorpay what actually happened to that payment link. Razorpay's API answer is
exactly as authoritative as its webhook — same source, different transport.

The pulled answer is stored as a `payment_events` row with source
`razorpay_api` so the outcome still names concrete evidence, and so an auditor
can see whether a given recovery was confirmed by push or by pull.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import asyncpg

from ..integrations.razorpay_client import RazorpayClient, RazorpayError
from .outcome import record_outcome

log = logging.getLogger(__name__)

PAID_STATUSES = frozenset({"paid"})
CLOSED_STATUSES = frozenset({"expired", "cancelled"})


@dataclass
class ReconcileResult:
    checked: int = 0
    newly_paid: int = 0
    newly_closed: int = 0
    recovered_minor: int = 0
    unchanged: int = 0
    errors: int = 0


async def reconcile_outcomes(
    conn: asyncpg.Connection,
    client: RazorpayClient,
    *,
    merchant_id: str,
    limit: int = 200,
) -> ReconcileResult:
    """Ask Razorpay what happened to executions we have no outcome for."""
    rows = await conn.fetch(
        """
        SELECT er.id, er.razorpay_ref, er.amount_minor, er.idempotency_key
          FROM execution_records er
          JOIN recovery_actions ra ON ra.id = er.action_id
          JOIN revenue_incidents i ON i.id = ra.incident_id
         WHERE i.merchant_id = $1
           AND er.status = 'succeeded'
           AND er.razorpay_ref IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM recovery_outcomes ro
                            WHERE ro.execution_id = er.id)
         ORDER BY er.id
         LIMIT $2
        """,
        merchant_id, limit,
    )

    result = ReconcileResult()
    for row in rows:
        result.checked += 1
        try:
            link = await client.fetch_payment_link(row["razorpay_ref"])
        except RazorpayError as e:
            log.warning("reconcile.fetch_failed ref=%s err=%s",
                        row["razorpay_ref"], e)
            result.errors += 1
            continue

        status = (link.get("status") or "").lower()
        if status not in PAID_STATUSES | CLOSED_STATUSES:
            result.unchanged += 1
            continue

        paid = status in PAID_STATUSES
        # Razorpay's own figure for what was actually paid, never ours.
        amount = int(link.get("amount_paid") or link.get("amount") or 0) if paid else 0

        # Record the pulled answer as evidence, so the outcome can name it and
        # an auditor can tell push from pull.
        event_id = f"rzp_api_{row['razorpay_ref']}"
        await conn.execute(
            """
            INSERT INTO payment_events
                (event_id, event_type, payload, signature_valid, source)
            VALUES ($1, $2, $3, FALSE, 'razorpay_api')
            ON CONFLICT (event_id) DO NOTHING
            """,
            event_id,
            "payment_link.paid" if paid else "payment_link.expired",
            json.dumps({"source": "reconciliation", "payment_link": link}),
        )

        outcome = await record_outcome(
            conn,
            event_id=event_id,
            event_type="payment_link.paid" if paid else "payment_link.expired",
            reference_id=row["idempotency_key"],
            payment_link_id=row["razorpay_ref"],
            amount_minor=amount,
            source="razorpay_api",
        )
        if not outcome.counted:
            result.unchanged += 1
            continue

        if paid:
            result.newly_paid += 1
            result.recovered_minor += outcome.recovered_minor
        else:
            result.newly_closed += 1

    log.info("reconcile.done checked=%d paid=%d recovered=%d",
             result.checked, result.newly_paid, result.recovered_minor)
    return result
