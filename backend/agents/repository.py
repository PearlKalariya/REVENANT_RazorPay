"""Persist investigation results.

The investigation is written together with an audit record, in one
transaction. An investigation that exists without an audit trail would break
the guarantee that every decision in the pipeline is traceable.
"""

from __future__ import annotations

import json

import asyncpg

from .investigation import InvestigationResult


async def persist_investigation(
    conn: asyncpg.Connection,
    *,
    incident_id: int,
    merchant_id: str,
    result: InvestigationResult,
    model: str,
    tool_calls: int,
) -> int:
    async with conn.transaction():
        investigation_id = await conn.fetchval(
            """
            INSERT INTO investigations
                (incident_id, root_cause, confidence, evidence, model)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            incident_id,
            result.root_cause,
            result.confidence,
            json.dumps(
                {
                    "evidence": result.evidence,
                    "affected_method": result.affected_method,
                    "dominant_failure_reason": result.dominant_failure_reason,
                    "is_transient": result.is_transient,
                    "recommended_focus": result.recommended_focus,
                    "tool_calls": tool_calls,
                }
            ),
            model,
        )
        await conn.execute(
            """
            UPDATE revenue_incidents SET status='investigating'
             WHERE id=$1 AND status='open'
            """,
            incident_id,
        )
        await conn.execute(
            """
            INSERT INTO audit_events
                (actor, event_type, merchant_id, incident_id, reason, metadata)
            VALUES ('INVESTIGATION_AGENT', 'ROOT_CAUSE_IDENTIFIED', $1, $2, $3, $4)
            """,
            merchant_id,
            incident_id,
            result.root_cause,
            json.dumps(
                {
                    "confidence": result.confidence,
                    "model": model,
                    "tool_calls": tool_calls,
                    "is_transient": result.is_transient,
                    "affected_method": result.affected_method,
                }
            ),
        )
    return investigation_id


async def load_latest_investigation(
    conn: asyncpg.Connection, incident_id: int
) -> tuple[InvestigationResult, str] | None:
    """Return the most recent stored investigation for an incident, if any.

    Re-investigating an incident that has already been investigated burns
    scarce free-tier quota (20 requests per model per day; one investigation
    costs roughly six) and produces the same answer, since the agent runs at
    temperature 0 over unchanged data.

    Returns (result, model) or None.
    """
    row = await conn.fetchrow(
        """
        SELECT root_cause, confidence, evidence, model
          FROM investigations
         WHERE incident_id = $1
         ORDER BY id DESC LIMIT 1
        """,
        incident_id,
    )
    if row is None:
        return None

    extra = row["evidence"]
    if isinstance(extra, str):
        extra = json.loads(extra)
    extra = extra or {}

    return (
        InvestigationResult(
            root_cause=row["root_cause"],
            confidence=row["confidence"],
            evidence=extra.get("evidence", []),
            affected_method=extra.get("affected_method"),
            dominant_failure_reason=extra.get("dominant_failure_reason"),
            is_transient=bool(extra.get("is_transient", False)),
            recommended_focus=extra.get("recommended_focus", ""),
        ),
        row["model"],
    )


async def persist_strategy(
    conn: asyncpg.Connection,
    *,
    incident_id: int,
    merchant_id: str,
    strategy,
    model: str,
    tool_calls: int,
) -> None:
    """Record the Recovery Agent's proposal in the audit ledger.

    The proposal is an auditable decision in its own right — "the agent
    suggested this, policy then ruled on it" — so the ledger is the correct
    home for it rather than a side table.

    It doubles as a cache: re-running the pipeline reuses the stored proposal
    instead of spending scarce free-tier quota to regenerate the same answer at
    temperature 0 over unchanged data.
    """
    await conn.execute(
        """
        INSERT INTO audit_events
            (actor, event_type, merchant_id, incident_id, reason, metadata)
        VALUES ('RECOVERY_AGENT', 'RECOVERY_STRATEGY_PROPOSED', $1, $2, $3, $4)
        """,
        merchant_id, incident_id, strategy.rationale,
        json.dumps({
            "action_type": strategy.normalized_action,
            "target_method": strategy.target_method,
            "target_failure_reasons": strategy.target_failure_reasons,
            "excluded_failure_reasons": strategy.excluded_failure_reasons,
            "expected_recovery_rate": strategy.expected_recovery_rate,
            "confidence": strategy.confidence,
            "model": model,
            "tool_calls": tool_calls,
        }),
    )


async def load_latest_strategy(conn: asyncpg.Connection, incident_id: int):
    """Most recent stored strategy for an incident, or None."""
    from .recovery import RecoveryStrategy

    row = await conn.fetchrow(
        """
        SELECT reason, metadata FROM audit_events
         WHERE incident_id = $1 AND event_type = 'RECOVERY_STRATEGY_PROPOSED'
         ORDER BY id DESC LIMIT 1
        """,
        incident_id,
    )
    if row is None:
        return None
    meta = row["metadata"]
    if isinstance(meta, str):
        meta = json.loads(meta)
    meta = meta or {}
    return (
        RecoveryStrategy(
            # `worth_recovering` was added to the schema after some stored
            # strategies were written, so old rows have no such key. Absence
            # must default to True (recover), never False (decline) — a
            # missing field silently defaulting to "don't act" would make an
            # old, perfectly good strategy look like a refusal it never made.
            worth_recovering=meta.get("worth_recovering", True),
            action_type=meta.get("action_type", "CREATE_PAYMENT_LINK"),
            target_method=meta.get("target_method"),
            target_failure_reasons=meta.get("target_failure_reasons", []),
            excluded_failure_reasons=meta.get("excluded_failure_reasons", []),
            rationale=row["reason"] or "",
            expected_recovery_rate=float(meta.get("expected_recovery_rate", 0.0)),
            confidence=float(meta.get("confidence", 0.0)),
        ),
        meta.get("model", "unknown"),
    )
