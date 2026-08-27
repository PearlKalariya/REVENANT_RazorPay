"""API security regression tests.

These exist because this system WAS exploited. With ENABLE_DEV_ENDPOINTS
defaulting to true and the app behind a public Cloudflare tunnel, an
unauthenticated caller injected a forged "payment succeeded" event for
₹500,000 which stored with signature_valid = TRUE.

The dev replay endpoint self-signs payloads, so reaching it is equivalent to
knowing the webhook secret. It is now guarded by two INDEPENDENT controls, and
each is tested separately — a single control with a plausible failure mode is
not a control.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.config import Settings, get_settings
from backend.main import app

FORGED = {
    "event_id": "evt_TEST_FORGED",
    "payload": {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {"entity": {"id": "p", "amount": 50_000_000,
                                        "status": "paid"}},
            "payment": {"entity": {"id": "pay_SYN00001", "amount": 50_000_000,
                                   "status": "captured"}},
        },
    },
}


class FakeRequest:
    """Minimal stand-in; the guards only read .client, .headers, .url."""

    class _Client:
        def __init__(self, host): self.host = host

    class _URL:
        path = "/dev/replay-webhook"

    def __init__(self, host: str, headers: dict | None = None):
        self.client = self._Client(host)
        self.headers = headers or {}
        self.url = self._URL()


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _use(**kw):
    """Install a Settings instance for the duration of one test."""
    base = dict(enable_dev_endpoints=True, api_key="correct-horse-battery",
                razorpay_mode="test")
    base.update(kw)
    get_settings.cache_clear()
    settings = Settings(**base)
    get_settings.__wrapped__.__defaults__ = ()
    app.dependency_overrides.clear()
    return settings


# --- layer 1: loopback ----------------------------------------------------


def test_loopback_detection():
    from backend.security import is_loopback
    assert is_loopback(FakeRequest("127.0.0.1"))
    assert is_loopback(FakeRequest("::1"))
    assert not is_loopback(FakeRequest("52.66.75.174"))
    assert not is_loopback(FakeRequest("10.0.0.5"))
    assert not is_loopback(FakeRequest(""))


def test_forwarded_for_header_is_not_trusted(monkeypatch):
    """X-Forwarded-For is attacker-controlled. Trusting it would restore the
    exact bypass this control closes."""
    import backend.security as sec
    monkeypatch.setattr(sec, "get_settings", lambda: _use())
    req = FakeRequest("52.66.75.174", {"x-forwarded-for": "127.0.0.1"})
    with pytest.raises(HTTPException) as e:
        sec.require_local_dev(req)
    assert e.value.status_code == 404


def test_remote_blocked_even_when_flag_enabled(monkeypatch):
    """Layer 1 alone must hold under the worst-case misconfiguration."""
    import backend.security as sec
    monkeypatch.setattr(sec, "get_settings", lambda: _use(enable_dev_endpoints=True))
    with pytest.raises(HTTPException) as e:
        sec.require_local_dev(FakeRequest("52.66.75.174"))
    assert e.value.status_code == 404


def test_disabled_flag_returns_404_not_403(monkeypatch):
    """404 does not confirm the endpoint exists."""
    import backend.security as sec
    monkeypatch.setattr(sec, "get_settings", lambda: _use(enable_dev_endpoints=False))
    with pytest.raises(HTTPException) as e:
        sec.require_local_dev(FakeRequest("127.0.0.1"))
    assert e.value.status_code == 404


def test_loopback_allowed_when_enabled(monkeypatch):
    import backend.security as sec
    monkeypatch.setattr(sec, "get_settings", lambda: _use(enable_dev_endpoints=True))
    assert sec.require_local_dev(FakeRequest("127.0.0.1")) is None


# --- layer 2: api key -----------------------------------------------------


def test_missing_api_key_rejected(monkeypatch):
    import backend.security as sec
    monkeypatch.setattr(sec, "get_settings", lambda: _use())
    with pytest.raises(HTTPException) as e:
        sec.require_api_key(FakeRequest("127.0.0.1"))
    assert e.value.status_code == 401


def test_wrong_api_key_rejected(monkeypatch):
    import backend.security as sec
    monkeypatch.setattr(sec, "get_settings", lambda: _use())
    with pytest.raises(HTTPException) as e:
        sec.require_api_key(FakeRequest("127.0.0.1", {"x-api-key": "wrong"}))
    assert e.value.status_code == 401


def test_correct_api_key_accepted(monkeypatch):
    import backend.security as sec
    monkeypatch.setattr(sec, "get_settings", lambda: _use())
    req = FakeRequest("127.0.0.1", {"x-api-key": "correct-horse-battery"})
    assert sec.require_api_key(req) is None


def test_unset_server_key_fails_closed(monkeypatch):
    """An unset key must never mean 'allow everyone'."""
    import backend.security as sec
    monkeypatch.setattr(sec, "get_settings", lambda: _use(api_key=""))
    with pytest.raises(HTTPException) as e:
        sec.require_api_key(FakeRequest("127.0.0.1", {"x-api-key": "anything"}))
    assert e.value.status_code == 503


def test_api_key_comparison_is_constant_time():
    """A plain == leaks how many leading characters matched, which is enough
    to recover the key byte by byte."""
    import inspect
    import backend.security as sec
    src = inspect.getsource(sec.require_api_key)
    assert "compare_digest" in src


# --- safe defaults --------------------------------------------------------


def test_dev_endpoints_default_to_disabled():
    assert Settings(_env_file=None).enable_dev_endpoints is False


def test_api_docs_default_to_disabled():
    """A public schema advertises the dev surface."""
    assert Settings(_env_file=None).enable_api_docs is False


def test_no_default_api_key():
    """A shipped default key is the same as no key."""
    assert Settings(_env_file=None).api_key == ""


def test_live_mode_is_refused_at_construction():
    """Refused when Settings is BUILT, not merely when get_settings() is used.

    Previously the check lived only in get_settings(), so any code building
    Settings() directly slipped past it. A financial guard that depends on
    callers choosing the right factory is not a guard.
    """
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="refused"):
        Settings(_env_file=None, razorpay_mode="live")


def test_live_mode_refused_case_insensitively():
    from pydantic import ValidationError
    for variant in ("live", "LIVE", "Live"):
        with pytest.raises(ValidationError, match="refused"):
            Settings(_env_file=None, razorpay_mode=variant)


# --- public surface -------------------------------------------------------


def test_health_requires_no_auth():
    """Health must stay open for probes; it exposes no secret values."""
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


def test_health_deep_never_returns_secret_values():
    from backend.config import get_settings as gs
    gs.cache_clear()
    s = gs()
    with TestClient(app) as client:
        body = client.get("/health/deep").text
    for secret in (s.razorpay_key_secret, s.razorpay_webhook_secret,
                   s.google_api_key, s.anthropic_api_key, s.api_key):
        if secret:
            assert secret not in body


# --- rate-limit backoff ---------------------------------------------------


def test_retry_delay_is_parsed_from_provider_error():
    """The provider tells us how long to wait. Guessing instead is how a run
    dies with the answer sitting in the error message."""
    from backend.agents.llm import _retry_after_seconds

    gemini = (
        "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You "
        "exceeded your current quota... Please retry in 29.34688745s.', "
        "'details': [{'@type': '...RetryInfo', 'retryDelay': '29s'}]}}"
    )
    assert _retry_after_seconds(gemini) == pytest.approx(29.0, abs=1.0)
    assert _retry_after_seconds(Exception("no delay here")) is None


def test_rate_limit_is_recognised_across_phrasings():
    from backend.agents.llm import _is_rate_limit

    for msg in ("429 RESOURCE_EXHAUSTED", "rate limit exceeded",
                "Quota exceeded for metric", "RESOURCE_EXHAUSTED"):
        assert _is_rate_limit(Exception(msg)), msg
    assert not _is_rate_limit(Exception("connection refused"))


def test_backoff_ceiling_exceeds_a_minute():
    """Free-tier quotas are per-minute, so a ceiling under 60s cannot clear one."""
    from backend.agents.llm import MAX_BACKOFF_SECONDS
    assert MAX_BACKOFF_SECONDS > 60


def test_daily_quota_distinguished_from_rate_limit():
    """A per-minute limit is worth waiting out. A daily quota is not — waiting
    60s for something that resets tomorrow just burns the demo."""
    from backend.agents.llm import is_quota_exhausted

    per_day = "'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'"
    per_min = "'quotaId': 'GenerateRequestsPerMinutePerProjectPerModel-FreeTier'"
    assert is_quota_exhausted(Exception(per_day))
    assert not is_quota_exhausted(Exception(per_min))


def test_fallback_chain_has_multiple_models():
    """Free quotas are per model per day, so one exhausted model must not end
    the run.

    Only two models qualify: the chain is restricted to models verified to
    support the full agent flow, and correctness beats chain length.
    """
    from backend.agents.llm import FALLBACK_MODELS
    assert len(FALLBACK_MODELS["google"]) >= 2


def test_pinned_model_disables_fallback():
    """If an operator pinned a model, silently using a different one is worse
    than failing."""
    from backend.agents.llm import model_chain
    from backend.config import Settings

    pinned = Settings(_env_file=None, llm_provider="google",
                      google_api_key="x", llm_model="gemini-2.5-flash")
    assert model_chain(pinned) == ["gemini-2.5-flash"]

    unpinned = Settings(_env_file=None, llm_provider="google", google_api_key="x")
    assert len(model_chain(unpinned)) > 1


def test_fallback_chain_excludes_incompatible_models():
    """lite models reject LangChain's structured-output prefill outright, so
    they cannot run this agent no matter the token budget."""
    from backend.agents.llm import FALLBACK_MODELS

    for model in FALLBACK_MODELS["google"]:
        assert "lite" not in model, f"{model} cannot run the agent flow"


def test_output_budget_accommodates_thinking_models():
    """gemini-3.x flash models spend output tokens on internal reasoning and
    return EMPTY content if the budget is small — which looks like a broken
    model but is just an insufficient cap."""
    from backend.agents.llm import MAX_OUTPUT_TOKENS
    assert MAX_OUTPUT_TOKENS >= 4096


def test_model_unavailable_triggers_fallback():
    """Model availability varies by project — one key listed gemini-2.5-flash
    and another 404'd on it. That must move to the next model, not fail."""
    from backend.agents.llm import is_model_unavailable

    assert is_model_unavailable(Exception("404 NOT_FOUND model not found"))
    assert not is_model_unavailable(Exception("429 RESOURCE_EXHAUSTED"))
    assert not is_model_unavailable(Exception("500 internal"))


def test_provider_overload_is_recognised():
    """A 503 is neither our bug nor a quota problem — the model is busy.
    Retrying briefly then falling through keeps a live demo alive."""
    from backend.agents.llm import is_transient_server_error

    assert is_transient_server_error(
        Exception("503 UNAVAILABLE. This model is currently experiencing high demand.")
    )
    assert is_transient_server_error(Exception("500 INTERNAL"))
    assert not is_transient_server_error(Exception("429 RESOURCE_EXHAUSTED"))
    assert not is_transient_server_error(Exception("400 INVALID_ARGUMENT"))
