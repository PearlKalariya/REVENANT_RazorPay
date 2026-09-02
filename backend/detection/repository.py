"""Persist detected incidents.

Detection itself is pure (`engine.detect`). This module is the only place it
touches the database, which keeps the detection logic testable without I/O.

Re-running detection must not create duplicate incidents. A merchant staring
at three copies of the same UPI outage has no idea which one to act on, and
each copy would spawn its own recovery actions. Identity is
(merchant, method, window_start), enforced on write.
"""

from __future__ import annotations

import asyncpg

from .engine import DetectionConfig, Incident, PaymentRecord, detect


async def load_payments(conn: asyncpg.Connection, merchant_id: str) -> list[PaymentRecord]:
    rows = await conn.fetch(
        """
        SELECT id, customer_id, amount_minor, status::text AS status,
               method, created_at, failure_reason
        FROM payments
        WHERE merchant_id = $1
        """,
        merchant_id,
    )
    return [
        PaymentRecord(
            id=r["id"],
            customer_id=r["customer_id"],
            amount_minor=r["amount_minor"],
            status=r["status"],
            method=r["method"],
            created_at=r["created_at"],
            failure_reason=r["failure_reason"],
        )
        for r in rows
    ]


async def persist_incident(
    conn: asyncpg.Connection, merchant_id: str, incident: Incident
) -> tuple[int, bool]:
    """Insert or update one incident. Returns (incident_id, created).

    Idempotent on (merchant, title, window_start). A re-scan refreshes the
    figures on the existing incident rather than creating a second one.
    """
    existing = await conn.fetchval(
        """
        SELECT id FROM revenue_incidents
        WHERE merchant_id = $1 AND title = $2 AND detected_at = $3
        """,
        merchant_id,
        incident.title,
        incident.window_start,
    )

    if existing is not None:
        await conn.execute(
            """
            UPDATE revenue_incidents
               SET revenue_at_risk_minor = $2, affected_count = $3
             WHERE id = $1
            """,
            existing,
            incident.revenue_at_risk_minor,
            incident.affected_count,
        )
        return existing, False

    incident_id = await conn.fetchval(
        """
        INSERT INTO revenue_incidents
            (merchant_id, title, status, revenue_at_risk_minor,
             affected_count, detected_at)
        VALUES ($1, $2, 'open', $3, $4, $5)
        RETURNING id
        """,
        merchant_id,
        incident.title,
        incident.revenue_at_risk_minor,
        incident.affected_count,
        incident.window_start,
    )

    await conn.execute(
        """
        INSERT INTO audit_events
            (actor, event_type, merchant_id, incident_id, amount_minor, reason,
             metadata)
        VALUES ('SYSTEM', 'REVENUE_INCIDENT_DETECTED', $1, $2, $3, $4, $5)
        """,
        merchant_id,
        incident_id,
        incident.revenue_at_risk_minor,
        f"{incident.affected_count} failed payments, "
        f"{incident.observed_failure_rate:.1%} failure rate vs "
        f"{incident.baseline_failure_rate:.1%} baseline",
        __import__("json").dumps(
            {
                "method": incident.method,
                "window_start": incident.window_start.isoformat(),
                "window_end": incident.window_end.isoformat(),
                "observed_failure_rate": incident.observed_failure_rate,
                "baseline_failure_rate": incident.baseline_failure_rate,
                "severity_multiple": round(incident.severity_multiple, 2),
                "top_failure_reason": incident.top_failure_reason,
                "affected_payment_ids": incident.affected_payment_ids,
            }
        ),
    )
    return incident_id, True


async def scan_and_persist(
    conn: asyncpg.Connection,
    merchant_id: str,
    config: DetectionConfig | None = None,
) -> list[tuple[int, Incident, bool]]:
    """Run detection over a merchant's payments and persist the results."""
    payments = await load_payments(conn, merchant_id)
    incidents = detect(payments, config)
    out = []
    for inc in incidents:
        incident_id, created = await persist_incident(conn, merchant_id, inc)
        out.append((incident_id, inc, created))
    return out
