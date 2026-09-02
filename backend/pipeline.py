"""End-to-end recovery pipeline.

Detect → investigate → propose → policy → execute, over a whole batch.

This is the path the demo walks and the path Track 03 asks to be measured:
money recovered across a batch, with stopping rules and an audit trail.

Every stage is resumable. Detection is idempotent, investigations are reused
rather than re-run (free-tier LLM quota is scarce), planning skips payments
that already have an action, and execution is idempotent per action. Running
the pipeline twice does not duplicate work or money.

Run:  python -m backend.pipeline
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import db
from .agents.investigation import investigate
from .agents.llm import resolve_model_name
from .agents.recovery import propose_strategy
from .agents.repository import (
    load_latest_investigation,
    load_latest_strategy,
    persist_investigation,
    persist_strategy,
)
from .config import get_settings
from .detection.repository import scan_and_persist
from .integrations.razorpay_client import RazorpayClient
from .policy import format_money, load_merchant_config
from .recovery.candidates import build_plan
from .recovery.executor import (
    ExecutionRefused,
    execute_action,
    resolve_pending_execution,
)
from .recovery.reconcile import reconcile_outcomes
from .recovery.repository import persist_plan

log = logging.getLogger(__name__)

#: Gap between money-moving calls. Razorpay test mode throttles bursts.
RAZORPAY_PACING_SECONDS = 2.5


@dataclass
class BatchResult:
    incidents: int = 0
    revenue_at_risk_minor: int = 0
    candidates: int = 0
    auto_approved: int = 0
    requires_approval: int = 0
    blocked_at_planning: int = 0
    blocked_reasons: dict[str, int] = field(default_factory=dict)
    executed: int = 0
    execution_pending: int = 0
    execution_failed: int = 0
    refused_at_execution: dict[str, int] = field(default_factory=dict)
    attempted_minor: int = 0
    #: Money PROVEN recovered — set by the Outcome Engine from verified
    #: Razorpay webhooks only. Zero here does NOT mean failure: it means no
    #: customer has paid a link yet.
    recovered_minor: int = 0
    links: list[str] = field(default_factory=list)


async def run_batch(
    merchant_id: str = "m_demo",
    *,
    execute: bool = True,
    limit: int | None = None,
) -> BatchResult:
    settings = get_settings()
    pool = await db.connect()
    result = BatchResult()

    async with pool.acquire() as conn:
        merchant = await load_merchant_config(conn, merchant_id)
        log.info("pipeline.start merchant=%s tz=%s currency=%s",
                 merchant_id, merchant.timezone, merchant.currency)

        # --- 1. detection (deterministic) ---------------------------------
        detected = await scan_and_persist(conn, merchant_id)
        result.incidents = len(detected)
        result.revenue_at_risk_minor = sum(
            inc.revenue_at_risk_minor for _, inc, _ in detected)
        if not detected:
            return result

        incident_id, incident, _ = detected[0]

        # --- 2. investigation (AI, read-only) -----------------------------
        stored = await load_latest_investigation(conn, incident_id)
        if stored is not None:
            investigation, model = stored
            log.info("pipeline.investigation reused model=%s", model)
        else:
            investigation, tool_calls = await investigate(
                pool, merchant_id, incident_id, settings)
            model = resolve_model_name(settings)
            await persist_investigation(
                conn, incident_id=incident_id, merchant_id=merchant_id,
                result=investigation, model=model, tool_calls=tool_calls)

        # --- 3. strategy (AI, propose-only) -------------------------------
        # Reused when already proposed: the agent runs at temperature 0 over
        # unchanged data, so regenerating spends scarce quota to obtain the
        # same answer.
        stored_strategy = await load_latest_strategy(conn, incident_id)
        if stored_strategy is not None:
            strategy, strategy_model = stored_strategy
            log.info("pipeline.strategy reused model=%s", strategy_model)
        else:
            strategy, tool_calls = await propose_strategy(
                pool, merchant_id, incident_id, investigation, settings)
            await persist_strategy(
                conn, incident_id=incident_id, merchant_id=merchant_id,
                strategy=strategy, model=resolve_model_name(settings),
                tool_calls=tool_calls)

        # --- 4. planning + policy (deterministic) -------------------------
        plan = await build_plan(
            conn, merchant_id=merchant_id, incident_id=incident_id,
            strategy=strategy, is_transient=investigation.is_transient,
            config=merchant.policy)
        result.candidates = len(plan.candidates)
        result.auto_approved = len(plan.auto)
        result.requires_approval = len(plan.approval)
        result.blocked_at_planning = len(plan.blocked)
        result.blocked_reasons = plan.blocked_by_rule()
        await persist_plan(conn, merchant_id=merchant_id,
                           incident_id=incident_id, plan=plan,
                           config=merchant.policy)

    if not execute:
        return result

    # --- 5. execution ------------------------------------------------------
    # Policy is re-evaluated per action against CURRENT state (D13), so the
    # batch stops itself the moment the daily cap is reached.
    client = RazorpayClient(settings)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT ra.id FROM recovery_actions ra
              JOIN revenue_incidents i ON i.id = ra.incident_id
             WHERE i.merchant_id = $1 AND ra.status = 'approved'
             ORDER BY ra.recovery_score DESC NULLS LAST, ra.id
            """,
            merchant_id,
        )
    action_ids = [r["id"] for r in rows][: limit or len(rows)]

    for index, action_id in enumerate(action_ids):
        # Razorpay rate-limits bursts. Pacing is cheaper and kinder than
        # relying on retries after being throttled.
        if index:
            await asyncio.sleep(RAZORPAY_PACING_SECONDS)
        async with pool.acquire() as conn:
            try:
                outcome = await execute_action(
                    conn, client, action_id=action_id,
                    merchant_id=merchant_id, settings=settings)
            except ExecutionRefused as e:
                result.refused_at_execution[e.rule] = (
                    result.refused_at_execution.get(e.rule, 0) + 1)
                continue

        if outcome.status == "succeeded":
            result.executed += 1
            if outcome.short_url:
                result.links.append(outcome.short_url)
        elif outcome.status == "pending":
            result.execution_pending += 1
        else:
            result.execution_failed += 1

    # --- 6. resolve anything left ambiguous ---------------------------------
    # A pending execution means we called Razorpay and do not know whether it
    # landed. Leaving them pending would understate the batch; guessing would
    # either fabricate revenue or invite a duplicate charge. Ask Razorpay.
    async with pool.acquire() as conn:
        pending_ids = [r["id"] for r in await conn.fetch(
            """
            SELECT er.id FROM execution_records er
              JOIN recovery_actions ra ON ra.id = er.action_id
              JOIN revenue_incidents i ON i.id = ra.incident_id
             WHERE i.merchant_id = $1 AND er.status = 'pending'
             ORDER BY er.id
            """, merchant_id)]

    for index, execution_id in enumerate(pending_ids):
        if index:
            await asyncio.sleep(RAZORPAY_PACING_SECONDS)
        async with pool.acquire() as conn:
            outcome = await resolve_pending_execution(
                conn, client, execution_id=execution_id)
        if outcome.status == "succeeded":
            result.executed += 1
            result.execution_pending = max(0, result.execution_pending - 1)
            if outcome.short_url:
                result.links.append(outcome.short_url)
        elif outcome.status == "failed":
            result.execution_failed += 1
            result.execution_pending = max(0, result.execution_pending - 1)

    # --- 6b. reconcile outcomes --------------------------------------------
    # Webhooks must never be assumed to arrive. Five real payments were once
    # confirmed by Razorpay while zero webhooks reached this system, because
    # the dashboard held a stale URL. Pull the authoritative answer too.
    async with pool.acquire() as conn:
        reconciled = await reconcile_outcomes(conn, client, merchant_id=merchant_id)
    if reconciled.newly_paid:
        log.info("pipeline.reconciled newly_paid=%d recovered=%d",
                 reconciled.newly_paid, reconciled.recovered_minor)

    # --- 7. report ACTUAL state, not this run's loop counters --------------
    # The counters above track what THIS invocation did. A batch is resumable,
    # so a later run would report "executed: 5" while 15 links existed — true
    # of the run, misleading as a result. The demo needs the real total.
    async with pool.acquire() as conn:
        totals = await conn.fetch(
            """
            SELECT er.status::text AS status, count(*) AS n,
                   coalesce(sum(er.amount_minor),0) AS paise
              FROM execution_records er
              JOIN recovery_actions ra ON ra.id = er.action_id
              JOIN revenue_incidents i ON i.id = ra.incident_id
             WHERE i.merchant_id = $1
             GROUP BY 1
            """, merchant_id)
        by_status = {r["status"]: (r["n"], int(r["paise"])) for r in totals}
        result.executed = by_status.get("succeeded", (0, 0))[0]
        result.execution_pending = by_status.get("pending", (0, 0))[0]
        result.execution_failed = by_status.get("failed", (0, 0))[0]
        result.links = [r["razorpay_short_url"] for r in await conn.fetch(
            """
            SELECT er.razorpay_short_url FROM execution_records er
              JOIN recovery_actions ra ON ra.id = er.action_id
              JOIN revenue_incidents i ON i.id = ra.incident_id
             WHERE i.merchant_id = $1 AND er.razorpay_short_url IS NOT NULL
             ORDER BY er.id
            """, merchant_id)]

    async with pool.acquire() as conn:
        result.attempted_minor = int(await conn.fetchval(
            """
            SELECT coalesce(sum(er.amount_minor),0) FROM execution_records er
             WHERE er.status IN ('succeeded','pending')
               AND er.execution_policy_hash IS NOT NULL
            """) or 0)
        # Verified recoveries only. The Outcome Engine excludes replayed
        # events, so this figure cannot be inflated by anything but a real
        # Razorpay webhook.
        result.recovered_minor = int(await conn.fetchval(
            "SELECT coalesce(sum(recovered_minor),0) FROM recovery_outcomes"
            " WHERE succeeded") or 0)

    return result


def _fmt(amount_minor, currency: str = "INR") -> str:
    """Display helper. The unit comes from the currency, not the field name."""
    return format_money(amount_minor, currency)


def render(r: BatchResult) -> str:
    lines = [
        "=" * 62,
        "  REVENANT — batch recovery run   (SYNTHETIC TEST DATA)",
        "=" * 62,
        f"  incidents detected      : {r.incidents}",
        f"  revenue at risk         : {_fmt(r.revenue_at_risk_minor)}",
        "",
        f"  recovery candidates     : {r.candidates}",
        f"    AUTO_APPROVED         : {r.auto_approved}",
        f"    REQUIRES_APPROVAL     : {r.requires_approval}",
        f"    BLOCKED (planning)    : {r.blocked_at_planning}",
    ] + [
        f"      {rule:22} {n}" for rule, n in sorted(r.blocked_reasons.items())
    ] + [
        "",
        f"  executed                : {r.executed}",
        f"  pending                 : {r.execution_pending}",
        f"  failed                  : {r.execution_failed}",
    ]
    if r.refused_at_execution:
        lines.append("  refused at execution    :")
        for rule, n in sorted(r.refused_at_execution.items()):
            lines.append(f"    {rule:24} {n}")
    lines += [
        "",
        f"  payment links issued    : {len(r.links)}",
        f"  recovery attempted      : {_fmt(r.attempted_minor)}",
        f"  RECOVERED (verified)    : {_fmt(r.recovered_minor)}",
    ]
    if r.recovered_minor == 0 and r.executed:
        lines.append("    ^ links issued; nothing paid yet. Recovered revenue is")
        lines.append("      counted only from verified Razorpay webhooks.")
    lines.append("=" * 62)
    return "\n".join(lines)


async def _main() -> None:
    p = argparse.ArgumentParser(description="Run the REVENANT recovery pipeline.")
    p.add_argument("--merchant", default="m_demo")
    p.add_argument("--dry-run", action="store_true",
                   help="plan and apply policy, but move no money")
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="  %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    result = await run_batch(args.merchant, execute=not args.dry_run,
                             limit=args.limit)
    print(render(result))
    await db.disconnect()


if __name__ == "__main__":
    asyncio.run(_main())
