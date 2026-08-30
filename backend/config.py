"""Application settings.

Loaded from environment / .env. Secrets are held here but NEVER logged,
serialised into responses, or returned by health endpoints. Health checks
report *presence* and *mode*, never values.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import model_validator
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

    # LLM provider. See docs/DECISIONS.md D4 (revised).
    llm_provider: str = "google"          # google | anthropic
    anthropic_api_key: str = ""
    google_api_key: str = ""
    llm_model: str = ""                   # blank = provider default

    # Gates the Failure Lab and webhook replay endpoints.
    # Defaults to FALSE. This previously defaulted to True and the app was
    # exploited through a public tunnel as a result: an unauthenticated caller
    # injected a forged "payment succeeded" event. Dev surface is now opt-in
    # AND loopback-only (see backend/security.py).
    enable_dev_endpoints: bool = False

    #: Shared secret for mutating endpoints (decision D9). No default:
    #: an unset key fails closed rather than allowing everyone.
    api_key: str = ""

    #: Origins the browser dashboard may call from. Explicit list, never "*":
    #: a wildcard with credentialed requests is refused by browsers anyway, and
    #: an allow-all CORS policy on an endpoint that moves money is not a policy.
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    #: Expose /docs, /redoc and /openapi.json. Off by default so the API
    #: surface — including dev routes — is not advertised publicly.
    enable_api_docs: bool = False

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def llm_api_key(self) -> str:
        return (
            self.google_api_key
            if self.llm_provider == "google"
            else self.anthropic_api_key
        )

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def razorpay_configured(self) -> bool:
        return bool(self.razorpay_key_id and self.razorpay_key_secret)

    @property
    def is_live_mode(self) -> bool:
        """Live mode is out of scope and must never be reachable by accident."""
        return self.razorpay_mode.lower() == "live"

    @model_validator(mode="after")
    def _refuse_live_mode(self):
        """Refuse live mode at CONSTRUCTION, not at first access.

        This check previously lived in get_settings(), which meant any code
        path building Settings() directly slipped past it. A financial guard
        that depends on callers using the right factory is not a guard.
        """
        if self.is_live_mode:
            raise ValueError(
                "RAZORPAY_MODE=live is refused. REVENANT is a test-mode-only "
                "system. Enabling live mode requires explicit human "
                "authorization and a recorded decision in docs/DECISIONS.md."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    # The live-mode refusal is enforced by Settings itself (see
    # _refuse_live_mode), so it holds no matter how Settings is constructed.
    return Settings()
