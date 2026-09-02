"""Per-merchant configuration.

Limits, currency and the business day belong to the merchant, not to the
codebase. A single hardcoded timezone is wrong for every merchant except the
one it was written for.

    Merchant A   Asia/Kolkata      ₹25,000/day
    Merchant B   America/New_York  $5,000/day
    Merchant C   Europe/London     £3,000/day

Stored as JSONB on `merchants.policy_config`, layered over the code defaults so
a merchant only has to specify what differs. Unknown keys are rejected rather
than ignored: a typo'd `max_daily_recovery` that silently does nothing is worse
than a startup error, because the limit it was meant to set never takes effect.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, fields

import asyncpg

from .engine import PolicyConfig

log = logging.getLogger(__name__)

CONFIGURABLE_FIELDS = {f.name for f in fields(PolicyConfig)}


class MerchantConfigError(Exception):
    """The stored configuration is invalid. Fail loudly — a policy that silently
    falls back to defaults is a policy nobody is actually enforcing."""


@dataclass(frozen=True)
class MerchantConfig:
    merchant_id: str
    name: str
    policy: PolicyConfig

    @property
    def timezone(self) -> str:
        return self.policy.business_timezone

    @property
    def currency(self) -> str:
        return self.policy.currency


def build_policy(raw: dict | None) -> PolicyConfig:
    """Layer stored overrides on top of the code defaults."""
    overrides = dict(raw or {})
    unknown = set(overrides) - CONFIGURABLE_FIELDS
    if unknown:
        raise MerchantConfigError(
            f"Unknown policy keys {sorted(unknown)}. Valid keys: "
            f"{sorted(CONFIGURABLE_FIELDS)}"
        )
    try:
        config = PolicyConfig(**overrides)
    except TypeError as e:
        raise MerchantConfigError(f"Invalid policy configuration: {e}") from e

    # Validate the timezone now, at load time, rather than discovering it is
    # wrong the first time a daily cap is computed.
    from .engine import current_merchant_day
    from datetime import datetime, timezone as _tz

    try:
        current_merchant_day(datetime.now(_tz.utc), config.business_timezone)
    except ValueError as e:
        raise MerchantConfigError(str(e)) from e

    if config.max_auto_amount_minor > config.max_daily_recovery_minor:
        raise MerchantConfigError(
            "max_auto_amount_minor exceeds max_daily_recovery_minor — a single "
            "auto-approved action could not fit inside the daily cap."
        )
    return config


async def load_merchant_config(
    conn: asyncpg.Connection, merchant_id: str
) -> MerchantConfig:
    """Load one merchant's configuration. Raises if the merchant is unknown."""
    row = await conn.fetchrow(
        "SELECT id, name, policy_config FROM merchants WHERE id = $1", merchant_id
    )
    if row is None:
        raise MerchantConfigError(f"Unknown merchant {merchant_id!r}.")

    raw = row["policy_config"]
    if isinstance(raw, str):
        raw = json.loads(raw)

    return MerchantConfig(
        merchant_id=row["id"], name=row["name"], policy=build_policy(raw)
    )
