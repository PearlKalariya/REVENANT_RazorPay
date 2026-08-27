"""Application settings.

Loaded from environment / .env. Secrets are held here but NEVER logged,
serialised into responses, or returned by health endpoints. Health checks
report *presence* and *mode*, never values.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    env: str = "development"
    log_level: str = "INFO"

    database_url: str = "postgresql://revive:revive@localhost:5433/revive"

    # Razorpay. Test mode only — see docs/DECISIONS.md D2.
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    razorpay_mode: str = "test"

    anthropic_api_key: str = ""

    # Gates the Failure Lab and webhook replay endpoints.
    enable_dev_endpoints: bool = True

    @property
    def razorpay_configured(self) -> bool:
        return bool(self.razorpay_key_id and self.razorpay_key_secret)

    @property
    def is_live_mode(self) -> bool:
        """Live mode is out of scope and must never be reachable by accident."""
        return self.razorpay_mode.lower() == "live"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    # Hard stop. Live mode requires explicit human authorization (spec §6).
    if settings.is_live_mode:
        raise RuntimeError(
            "RAZORPAY_MODE=live is refused. REVENANT is a test-mode-only system. "
            "Enabling live mode requires explicit human authorization and a "
            "recorded decision in docs/DECISIONS.md."
        )
    return settings
