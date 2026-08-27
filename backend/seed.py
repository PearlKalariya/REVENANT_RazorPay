"""Synthetic dataset generator.

Produces a realistic Indian payments dataset containing a detectable revenue
incident: a UPI timeout spike inside a defined window.

DETERMINISTIC. Seeded with a fixed value so every run produces identical data.
Metrics computed over this dataset are therefore reproducible and can be
re-verified by anyone — which is the whole point of the metrics-integrity rule.

Every row is written with is_synthetic = TRUE. Nothing here is real money,
and nothing downstream may present it as such.

Run:  python -m backend.seed
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone

import asyncpg

from .config import get_settings

SEED = 42
MERCHANT_ID = "m_demo"
MERCHANT_NAME = "Kirana Fresh (Synthetic Demo Merchant)"

N_CUSTOMERS = 150
N_PAYMENTS = 500
OPT_OUT_RATE = 0.06

# Window in which UPI degrades badly. This is the incident REVENANT must find.
SPIKE_START_HOUR = 14
SPIKE_END_HOUR = 17
SPIKE_UPI_FAILURE_RATE = 0.70
BASELINE_FAILURE_RATE = 0.045

METHODS = ["upi", "card", "netbanking"]
METHOD_WEIGHTS = [0.45, 0.32, 0.23]

FAILURE_REASONS = {
    "upi": [
        ("BAD_REQUEST_ERROR", "Payment timed out at the UPI PSP"),
        ("GATEWAY_ERROR", "UPI collect request expired"),
    ],
    "card": [
        ("BAD_REQUEST_ERROR", "Card declined by issuing bank"),
        ("BAD_REQUEST_ERROR", "Insufficient funds"),
    ],
    "netbanking": [
        ("GATEWAY_ERROR", "Bank gateway unavailable"),
    ],
}


def _amount_paise(rng: random.Random) -> int:
    """Realistic order values. Long tail so some land above the ₹5,000
    autonomous limit, which is what exercises the approval path."""
    bucket = rng.random()
    if bucket < 0.70:
        rupees = rng.randint(150, 900)        # everyday basket
    elif bucket < 0.93:
        rupees = rng.randint(900, 2_500)      # weekly shop
    else:
        rupees = rng.randint(5_200, 12_000)   # bulk / party order
    return rupees * 100


def build_dataset() -> tuple[list, list, dict]:
    rng = random.Random(SEED)
    base = datetime(2026, 8, 26, 0, 0, tzinfo=timezone.utc)

    customers = []
    for i in range(N_CUSTOMERS):
        customers.append(
            (
                f"cust_{i:04d}",
                MERCHANT_ID,
                f"customer{i:04d}@example.test",
                f"+9198{rng.randint(10**7, 10**8 - 1)}",
                rng.random() < OPT_OUT_RATE,
            )
        )

    payments = []
    stats = {
        "total": 0,
        "failed": 0,
        "captured": 0,
        "at_risk_paise": 0,
        "spike_upi_failed": 0,
    }

    for i in range(N_PAYMENTS):
        cust = rng.choice(customers)
        method = rng.choices(METHODS, weights=METHOD_WEIGHTS)[0]
        created = base + timedelta(
            hours=rng.randint(0, 23), minutes=rng.randint(0, 59)
        )
        in_spike = (
            method == "upi" and SPIKE_START_HOUR <= created.hour < SPIKE_END_HOUR
        )
        fail_rate = SPIKE_UPI_FAILURE_RATE if in_spike else BASELINE_FAILURE_RATE
        failed = rng.random() < fail_rate
        amount = _amount_paise(rng)

        code = reason = None
        if failed:
            status = "failed"
            code, reason = rng.choice(FAILURE_REASONS[method])
            stats["failed"] += 1
            stats["at_risk_paise"] += amount
            if in_spike:
                stats["spike_upi_failed"] += 1
        else:
            status = "captured"
            stats["captured"] += 1

        payments.append(
            (
                f"pay_SYN{i:05d}",
                MERCHANT_ID,
                cust[0],
                amount,
                "INR",
                status,
                method,
                reason,
                code,
                created,
                True,
            )
        )
        stats["total"] += 1

    # NOTE: the already-paid case is deliberately NOT baked into this dataset.
    #
    # A payment that is already 'captured' here would never be detected as
    # failed, so it would never reach the Policy Engine and would prove
    # nothing. The real scenario is a RACE: the payment looks failed when
    # detected, the customer pays by another means, and policy must block the
    # recovery at execution time.
    #
    # That is a runtime scenario, so it belongs to the Failure Lab, which flips
    # a payment to 'captured' after an action has been proposed. Static seed
    # data cannot express it honestly.

    return customers, payments, stats


async def seed() -> dict:
    settings = get_settings()
    conn = await asyncpg.connect(settings.database_url)
    try:
        customers, payments, stats = build_dataset()

        async with conn.transaction():
            # Idempotent: a re-seed replaces the synthetic dataset wholesale.
            await conn.execute("TRUNCATE merchants CASCADE")
            await conn.execute(
                "INSERT INTO merchants (id, name, policy_config) VALUES ($1,$2,$3)",
                MERCHANT_ID,
                MERCHANT_NAME,
                "{}",
            )
            await conn.executemany(
                "INSERT INTO customers (id, merchant_id, email, phone, opted_out)"
                " VALUES ($1,$2,$3,$4,$5)",
                customers,
            )
            await conn.executemany(
                "INSERT INTO payments (id, merchant_id, customer_id, amount_paise,"
                " currency, status, method, failure_reason, failure_code,"
                " created_at, is_synthetic)"
                " VALUES ($1,$2,$3,$4,$5,$6::payment_status,$7,$8,$9,$10,$11)",
                payments,
            )
        return stats
    finally:
        await conn.close()


def _rupees(paise: int) -> str:
    return f"₹{paise / 100:,.2f}"


if __name__ == "__main__":
    result = asyncio.run(seed())
    print("=" * 58)
    print("  SYNTHETIC TEST DATA — not real money, not real customers")
    print("=" * 58)
    print(f"  seed              : {SEED} (deterministic)")
    print(f"  customers         : {N_CUSTOMERS}")
    print(f"  payments          : {result['total']}")
    print(f"  captured          : {result['captured']}")
    print(f"  failed            : {result['failed']}")
    print(f"  revenue at risk   : {_rupees(result['at_risk_paise'])}")
    print(f"  UPI spike failures: {result['spike_upi_failed']}"
          f" (window {SPIKE_START_HOUR}:00-{SPIKE_END_HOUR}:00 UTC)")
    print("=" * 58)
