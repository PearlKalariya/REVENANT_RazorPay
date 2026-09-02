"""Read-only tools for the Investigation Agent.

Every tool here is SELECT-only. That is the security boundary, and it is
structural rather than advisory: there is no tool in this module that writes,
executes, approves, or contacts Razorpay. A capability that does not exist
cannot be reached by a hallucinating model or a prompt injection.

The Investigation Agent answers "why is revenue being lost?". It has no
business proposing or performing actions, so it is given no way to.

Amounts are returned in paise (D8) with rupee strings alongside, purely so the
model's narrative reads naturally. Policy never reads these strings.
"""

from __future__ import annotations

import json
from datetime import datetime

import asyncpg
from langchain_core.tools import StructuredTool

#: Guard used by tests and by the agent factory. Any tool name not in here is
#: refused before the graph is built.
ALLOWED_TOOL_NAMES = frozenset(
    {
        "get_incident_details",
        "get_failure_statistics",
        "get_merchant_baseline",
        "get_related_payments",
        "get_payment_history",
        "get_customer_history",
    }
)

#: Capabilities the Investigation Agent must never hold. Asserted in tests.
FORBIDDEN_CAPABILITIES = frozenset(
    {
        "create_payment_link",
        "retry_payment",
        "refund",
        "payout",
        "change_policy",
        "approve_action",
        "execute_action",
    }
)


def _rupees(paise) -> str:
    return "-" if paise is None else f"₹{int(paise) / 100:,.2f}"


def _int(value) -> int:
    """Postgres sum() over BIGINT returns numeric, which asyncpg gives back
    as Decimal — and Decimal is not JSON serialisable. Coerce at the
    boundary so a tool never dies mid-investigation."""
    return int(value or 0)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def build_tools(pool: asyncpg.Pool, merchant_id: str) -> list[StructuredTool]:
    """Build the read-only toolset, scoped to one merchant.

    `merchant_id` is bound here rather than passed as a tool argument, so the
    model cannot read another merchant's data by asking for it.
    """

    async def get_incident_details(incident_id: int) -> str:
        """Get a revenue incident: title, status, revenue at risk, affected count,
        detection window, and the detector's own evidence."""
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT i.id, i.title, i.status::text AS status,
                       i.revenue_at_risk_minor, i.affected_count, i.detected_at,
                       a.metadata, a.reason
                  FROM revenue_incidents i
                  LEFT JOIN audit_events a
                         ON a.incident_id = i.id
                        AND a.event_type = 'REVENUE_INCIDENT_DETECTED'
                 WHERE i.id = $1 AND i.merchant_id = $2
                """,
                incident_id,
                merchant_id,
            )
        if row is None:
            return json.dumps({"error": f"No incident {incident_id} for this merchant."})

        meta = row["metadata"]
        if isinstance(meta, str):
            meta = json.loads(meta)
        meta = meta or {}
        return json.dumps(
            {
                "incident_id": row["id"],
                "title": row["title"],
                "status": row["status"],
                "revenue_at_risk_minor": _int(row["revenue_at_risk_minor"]),
                "revenue_at_risk": _rupees(row["revenue_at_risk_minor"]),
                "affected_count": _int(row["affected_count"]),
                "detected_at": _iso(row["detected_at"]),
                "detector_summary": row["reason"],
                "method": meta.get("method"),
                "window_start": meta.get("window_start"),
                "window_end": meta.get("window_end"),
                "observed_failure_rate": meta.get("observed_failure_rate"),
                "baseline_failure_rate": meta.get("baseline_failure_rate"),
                "severity_multiple": meta.get("severity_multiple"),
                "top_failure_reason": meta.get("top_failure_reason"),
            },
            indent=2,
        )

    async def get_failure_statistics(method: str | None = None) -> str:
        """Failure counts and rates broken down by payment method and hour.
        Pass a method (upi/card/netbanking) to narrow, or omit for all."""
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT method,
                       date_trunc('hour', created_at) AS hour,
                       count(*) AS total,
                       count(*) FILTER (WHERE status = 'failed') AS failed,
                       sum(amount_minor) FILTER (WHERE status = 'failed')
                           AS failed_minor
                  FROM payments
                 WHERE merchant_id = $1
                   AND ($2::text IS NULL OR method = $2)
                 GROUP BY 1, 2
                 HAVING count(*) > 0
                 ORDER BY 2, 1
                """,
                merchant_id,
                method,
            )
        return json.dumps(
            [
                {
                    "method": r["method"],
                    "hour": _iso(r["hour"]),
                    "total": _int(r["total"]),
                    "failed": _int(r["failed"]),
                    "failure_rate": round(r["failed"] / r["total"], 4),
                    "failed_minor": _int(r["failed_minor"]),
                }
                for r in rows
            ],
            indent=2,
        )

    async def get_merchant_baseline() -> str:
        """Overall volume and failure rate per method across the WHOLE period,
        INCLUDING any incident windows.

        Important: this is NOT the same figure as the detector's baseline. The
        detector excludes anomalous windows, so its baseline is lower and is the
        correct 'normal' to compare a spike against. Use this tool for overall
        volume and method mix. When quoting a baseline failure rate, quote the
        detector's."""
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT method,
                       count(*) AS total,
                       count(*) FILTER (WHERE status = 'failed') AS failed,
                       sum(amount_minor) AS volume_minor
                  FROM payments
                 WHERE merchant_id = $1
                 GROUP BY 1 ORDER BY 1
                """,
                merchant_id,
            )
        return json.dumps(
            [
                {
                    "method": r["method"],
                    "total_payments": _int(r["total"]),
                    "failed": _int(r["failed"]),
                    "failure_rate_including_incidents": round(r["failed"] / r["total"], 4),
                    "volume": _rupees(r["volume_minor"]),
                }
                for r in rows
            ],
            indent=2,
        )

    async def get_related_payments(incident_id: int, limit: int = 25) -> str:
        """The failed payments belonging to an incident, with amounts and
        failure reasons. Use to see what actually broke."""
        async with pool.acquire() as conn:
            meta = await conn.fetchval(
                """
                SELECT metadata FROM audit_events
                 WHERE incident_id = $1 AND event_type = 'REVENUE_INCIDENT_DETECTED'
                 LIMIT 1
                """,
                incident_id,
            )
            if meta is None:
                return json.dumps({"error": f"No incident {incident_id}."})
            if isinstance(meta, str):
                meta = json.loads(meta)
            ids = (meta or {}).get("affected_payment_ids", [])[: max(1, min(limit, 100))]
            rows = await conn.fetch(
                """
                SELECT id, customer_id, amount_minor, method, failure_code,
                       failure_reason, created_at
                  FROM payments
                 WHERE id = ANY($1::text[]) AND merchant_id = $2
                 ORDER BY created_at
                """,
                ids,
                merchant_id,
            )
        return json.dumps(
            [
                {
                    "payment_id": r["id"],
                    "customer_id": r["customer_id"],
                    "amount_minor": _int(r["amount_minor"]),
                    "amount": _rupees(r["amount_minor"]),
                    "method": r["method"],
                    "failure_code": r["failure_code"],
                    "failure_reason": r["failure_reason"],
                    "created_at": _iso(r["created_at"]),
                }
                for r in rows
            ],
            indent=2,
        )

    async def get_payment_history(payment_id: str) -> str:
        """Full detail for one payment, plus any webhook events recorded for it."""
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, customer_id, amount_minor, status::text AS status,
                       method, failure_code, failure_reason, created_at
                  FROM payments WHERE id = $1 AND merchant_id = $2
                """,
                payment_id,
                merchant_id,
            )
            if row is None:
                return json.dumps({"error": f"No payment {payment_id}."})
            events = await conn.fetch(
                """
                SELECT event_type, received_at FROM payment_events
                 WHERE payment_id = $1 ORDER BY received_at
                """,
                payment_id,
            )
        return json.dumps(
            {
                "payment_id": row["id"],
                "customer_id": row["customer_id"],
                "amount_minor": _int(row["amount_minor"]),
                "amount": _rupees(row["amount_minor"]),
                "status": row["status"],
                "method": row["method"],
                "failure_code": row["failure_code"],
                "failure_reason": row["failure_reason"],
                "created_at": _iso(row["created_at"]),
                "events": [
                    {"type": e["event_type"], "at": _iso(e["received_at"])}
                    for e in events
                ],
            },
            indent=2,
        )

    async def get_customer_history(customer_id: str) -> str:
        """A customer's payment record: totals, successes, failures, and whether
        they have opted out of recovery contact."""
        async with pool.acquire() as conn:
            cust = await conn.fetchrow(
                "SELECT id, opted_out FROM customers WHERE id=$1 AND merchant_id=$2",
                customer_id,
                merchant_id,
            )
            if cust is None:
                return json.dumps({"error": f"No customer {customer_id}."})
            stats = await conn.fetchrow(
                """
                SELECT count(*) AS total,
                       count(*) FILTER (WHERE status='captured') AS succeeded,
                       count(*) FILTER (WHERE status='failed') AS failed,
                       sum(amount_minor) FILTER (WHERE status='captured')
                           AS lifetime_minor
                  FROM payments WHERE customer_id=$1
                """,
                customer_id,
            )
        return json.dumps(
            {
                "customer_id": cust["id"],
                # Surfaced so the model understands the constraint. It is still
                # the Policy Engine, not the model, that enforces it.
                "opted_out_of_recovery": cust["opted_out"],
                "total_payments": _int(stats["total"]),
                "succeeded": _int(stats["succeeded"]),
                "failed": _int(stats["failed"]),
                "lifetime_value": _rupees(_int(stats["lifetime_minor"])),
            },
            indent=2,
        )

    tools = [
        StructuredTool.from_function(coroutine=fn, name=fn.__name__,
                                     description=fn.__doc__)
        for fn in (
            get_incident_details,
            get_failure_statistics,
            get_merchant_baseline,
            get_related_payments,
            get_payment_history,
            get_customer_history,
        )
    ]

    # Structural guard: refuse to build a toolset containing anything not on
    # the read-only allowlist. Catches an accidental capability addition at
    # construction time rather than in production.
    for t in tools:
        if t.name not in ALLOWED_TOOL_NAMES:
            raise ValueError(f"Tool {t.name!r} is not on the read-only allowlist.")
    return tools
