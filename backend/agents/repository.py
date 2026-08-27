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
