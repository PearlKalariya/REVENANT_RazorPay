"""Per-merchant configuration tests.

Limits, currency and the business day belong to the merchant. A single
hardcoded timezone is wrong for every merchant except the one it was written
for.
"""

from datetime import datetime, timezone

import asyncpg
import json
import pytest

from backend.config import get_settings
from backend.policy import (
    MerchantConfigError,
    PolicyConfig,
    build_policy,
    current_merchant_day,
    load_merchant_config,
    merchant_day_start,
)


@pytest.fixture
async def conn():
    try:
        c = await asyncpg.connect(get_settings().database_url, timeout=3)
    except Exception:
        pytest.skip("Postgres not reachable — run `docker compose up -d db`")
    tx = c.transaction()
    await tx.start()
    try:
        yield c
    finally:
        await tx.rollback()
        await c.close()


# --- the merchant's day ---------------------------------------------------


def test_same_instant_is_a_different_day_per_merchant():
    """20:30 UTC is already tomorrow in Kolkata and still today in New York.
    A UTC-based cap resets mid-business-day for one of them, whichever default
    is picked — which is why it cannot be a default at all."""
    now = datetime(2026, 8, 28, 20, 30, tzinfo=timezone.utc)
    assert current_merchant_day(now, "Asia/Kolkata").isoformat() == "2026-08-29"
    assert current_merchant_day(now, "America/New_York").isoformat() == "2026-08-28"
    assert current_merchant_day(now, "Europe/London").isoformat() == "2026-08-28"


def test_day_start_differs_per_merchant():
    now = datetime(2026, 8, 28, 20, 30, tzinfo=timezone.utc)
    starts = {tz: merchant_day_start(now, tz)
              for tz in ("Asia/Kolkata", "America/New_York", "Europe/London")}
    assert len(set(starts.values())) == 3, "each merchant's day begins at its own instant"


def test_dst_is_handled():
    """A New York day is 23 hours long when DST begins. zoneinfo handles it;
    a fixed UTC offset would not."""
    before = merchant_day_start(datetime(2026, 3, 7, 12, 0, tzinfo=timezone.utc),
                                "America/New_York")
    after = merchant_day_start(datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc),
                               "America/New_York")
    assert before.utcoffset() == after.utcoffset() == timezone.utc.utcoffset(None)
    # The local offset changed across the DST boundary even though both are UTC.
    assert (after - before).total_seconds() % 86400 != 0


def test_unknown_timezone_fails_loudly():
    """A silent fallback to UTC would move a merchant's day boundary with no
    symptom until their cap is overspent weeks later."""
    with pytest.raises(ValueError, match="Unknown business_timezone"):
        current_merchant_day(datetime.now(timezone.utc), "Mars/Olympus_Mons")


# --- configuration layering -----------------------------------------------


def test_defaults_apply_when_nothing_stored():
    cfg = build_policy(None)
    assert cfg.currency == "INR"
    assert cfg.business_timezone == "Asia/Kolkata"
    assert cfg.max_daily_recovery_minor == 2_500_000


def test_merchant_overrides_are_layered_over_defaults():
    cfg = build_policy({"business_timezone": "America/New_York",
                        "currency": "USD",
                        "max_daily_recovery_minor": 500_000})
    assert cfg.business_timezone == "America/New_York"
    assert cfg.currency == "USD"
    assert cfg.max_daily_recovery_minor == 500_000
    assert cfg.max_retry_attempts == PolicyConfig().max_retry_attempts


def test_unknown_key_is_rejected_not_ignored():
    """A typo'd limit that silently does nothing is worse than a startup error:
    the limit it was meant to set never takes effect."""
    with pytest.raises(MerchantConfigError, match="Unknown policy keys"):
        build_policy({"max_daily_recovery": 500_000})   # missing _minor


def test_invalid_timezone_rejected_at_load():
    with pytest.raises(MerchantConfigError, match="Unknown business_timezone"):
        build_policy({"business_timezone": "Not/AZone"})


def test_auto_limit_above_daily_cap_rejected():
    """An auto-approve threshold larger than the daily cap means a single
    permitted action could never fit inside the day's budget."""
    with pytest.raises(MerchantConfigError, match="exceeds"):
        build_policy({"max_auto_amount_minor": 9_000_000,
                      "max_daily_recovery_minor": 2_500_000})


# --- loading from the database --------------------------------------------


async def test_three_merchants_three_configurations(conn):
    # (timezone, currency, daily cap, auto-approve threshold) in MINOR units.
    # The auto threshold must sit inside the daily cap — build_policy enforces
    # that, and an earlier version of this fixture tripped it by leaving the
    # GBP merchant on the INR default.
    merchants = {
        "m_in": ("Asia/Kolkata", "INR", 2_500_000, 500_000),
        "m_us": ("America/New_York", "USD", 500_000, 100_000),
        "m_uk": ("Europe/London", "GBP", 300_000, 50_000),
    }
    for mid, (tz, ccy, cap, auto) in merchants.items():
        await conn.execute(
            "INSERT INTO merchants(id,name,policy_config) VALUES($1,$2,$3)",
            mid, mid.upper(),
            json.dumps({"business_timezone": tz, "currency": ccy,
                        "max_daily_recovery_minor": cap,
                        "max_auto_amount_minor": auto}))

    now = datetime(2026, 8, 28, 20, 30, tzinfo=timezone.utc)
    days = {}
    for mid, (tz, ccy, cap, auto) in merchants.items():
        cfg = await load_merchant_config(conn, mid)
        assert cfg.timezone == tz
        assert cfg.currency == ccy
        assert cfg.policy.max_daily_recovery_minor == cap
        assert cfg.policy.max_auto_amount_minor == auto
        days[mid] = current_merchant_day(now, cfg.timezone)

    assert days["m_in"] != days["m_us"], (
        "at this instant the Indian merchant is on a later business day")


async def test_unknown_merchant_rejected(conn):
    with pytest.raises(MerchantConfigError, match="Unknown merchant"):
        await load_merchant_config(conn, "m_nope")


async def test_demo_merchant_has_explicit_config(conn):
    """The seeded merchant carries its own configuration rather than relying on
    code defaults, so the stored path is the one actually exercised.

    The daily cap here is ₹75,000 per decision D17 — raised from ₹25,000 when
    the dataset grew to ₹76,827 at risk and the old cap stopped being
    proportionate to the data it governs. The CODE default is still ₹25,000;
    only this merchant's stored row differs, which is the point of D14.
    """
    cfg = await load_merchant_config(conn, "m_demo")
    assert cfg.currency == "INR"
    assert cfg.timezone == "Asia/Kolkata"
    assert cfg.policy.max_daily_recovery_minor == 7_500_000     # D17
    assert cfg.policy.max_auto_amount_minor == 500_000          # unchanged
    assert cfg.policy.action_ttl_minutes == 24 * 60             # D18

    from backend.policy import PolicyConfig
    assert PolicyConfig().max_daily_recovery_minor == 2_500_000, (
        "the code default must not drift with one merchant's configuration")


def test_unsupported_currency_is_refused_not_silently_converted():
    """A merchant configured for USD must NOT be charged in INR.

    Currency was configurable but unenforced: the config said USD while the
    Razorpay call hardcoded INR, so the customer would have been billed in
    rupees with nothing in the system saying so.
    """
    import asyncio

    from backend.config import Settings
    from backend.integrations.razorpay_client import RazorpayClient, RazorpayError

    client = RazorpayClient(Settings(
        _env_file=None, razorpay_key_id="rzp_test_x", razorpay_key_secret="y",
        razorpay_mode="test"))

    with pytest.raises(RazorpayError, match="only settle"):
        asyncio.run(client.create_payment_link(
            amount_minor=1000, reference_id="rv_x", description="d",
            currency="USD"))
