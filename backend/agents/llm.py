"""LLM provider selection and structured-output hardening.

Decision D4 (revised): Gemini's free tier is the default provider. Anthropic
remains selectable via LLM_PROVIDER, so a later credit top-up is a one-line
env change rather than a refactor.

The hardening here exists because free-tier models are less consistent than
paid frontier models at emitting schema-valid structured output. That
inconsistency is a real risk in a financial system, so it is handled
explicitly instead of hoped away:

* **Validation is mandatory.** Downstream code reads typed fields, never
  prose. A malformed response raises rather than propagating a half-parsed
  object into recovery logic.
* **Retry on validation failure**, with the validation error fed back to the
  model. Most schema misses are self-correcting when the model is shown what
  it got wrong.
* **Fail closed.** If every attempt fails, the caller gets an exception. An
  incident with no investigation is a visible gap; an incident with a
  hallucinated investigation is a silent one.
* **Rate-limit aware.** Free tiers throttle. 429s back off and retry rather
  than surfacing as a hard failure mid-demo.
"""

from __future__ import annotations

import asyncio
import logging

from pydantic import BaseModel, ValidationError

from ..config import Settings

log = logging.getLogger(__name__)

DEFAULT_MODELS = {
    "google": "gemini-2.5-flash",
    "anthropic": "claude-sonnet-5",
}

MAX_SCHEMA_RETRIES = 3
MAX_RATELIMIT_RETRIES = 4


class LLMUnavailable(RuntimeError):
    """No usable model, or every attempt failed."""


def resolve_model_name(settings: Settings) -> str:
    if settings.llm_model:
        return settings.llm_model
    return DEFAULT_MODELS.get(settings.llm_provider, DEFAULT_MODELS["google"])


def build_model(settings: Settings, *, temperature: float = 0.0):
    """Build the chat model for the configured provider."""
    if not settings.llm_configured:
        raise LLMUnavailable(
            f"No API key for LLM_PROVIDER={settings.llm_provider!r}. "
            "Set GOOGLE_API_KEY (free: aistudio.google.com/apikey) "
            "or switch LLM_PROVIDER."
        )

    model_name = resolve_model_name(settings)

    if settings.llm_provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=model_name,
            google_api_key=settings.google_api_key,
            temperature=temperature,
            # Investigation must be reproducible; sampling variety is not a
            # virtue when the output feeds a financial pipeline.
            max_retries=2,
        )

    if settings.llm_provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=model_name,
            api_key=settings.anthropic_api_key,
            temperature=temperature,
            max_tokens=4096,
        )

    raise LLMUnavailable(f"Unknown LLM_PROVIDER {settings.llm_provider!r}.")


def _is_rate_limit(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return "429" in text or "rate limit" in text or "resource_exhausted" in text or "quota" in text


async def invoke_with_validation(
    agent,
    messages: list[dict],
    schema: type[BaseModel],
) -> tuple[BaseModel, dict]:
    """Invoke an agent and guarantee a schema-valid result, or raise.

    Returns (validated_result, state). Never returns a partially parsed object.
    """
    last_error: Exception | None = None
    convo = list(messages)

    for attempt in range(1, MAX_SCHEMA_RETRIES + 1):
        state = await _invoke_with_backoff(agent, convo)

        raw = state.get("structured_response")
        if isinstance(raw, schema):
            return raw, state

        try:
            validated = schema.model_validate(
                raw if isinstance(raw, dict) else getattr(raw, "__dict__", {})
            )
            return validated, state
        except ValidationError as e:
            last_error = e
            log.warning(
                "llm.schema_invalid attempt=%d/%d errors=%d",
                attempt, MAX_SCHEMA_RETRIES, len(e.errors()),
            )
            if attempt < MAX_SCHEMA_RETRIES:
                # Show the model exactly what it got wrong. Most schema
                # misses self-correct on the next pass.
                convo = list(messages) + [
                    {
                        "role": "user",
                        "content": (
                            "Your previous response did not match the required "
                            f"schema:\n{e}\n\nReturn a response that satisfies "
                            "every field constraint. Ground all figures in tool "
                            "results — do not invent values to satisfy the schema."
                        ),
                    }
                ]

    raise LLMUnavailable(
        f"Model failed to produce schema-valid output after "
        f"{MAX_SCHEMA_RETRIES} attempts. Last error: {last_error}"
    )


async def _invoke_with_backoff(agent, messages: list[dict]) -> dict:
    """Invoke, retrying rate limits with exponential backoff.

    Free tiers throttle. A 429 mid-demo should cost a couple of seconds, not
    the run.
    """
    delay = 2.0
    last: Exception | None = None

    for attempt in range(1, MAX_RATELIMIT_RETRIES + 1):
        try:
            return await agent.ainvoke({"messages": messages})
        except Exception as e:  # noqa: BLE001 - provider SDKs raise varied types
            last = e
            if not _is_rate_limit(e) or attempt == MAX_RATELIMIT_RETRIES:
                raise
            log.warning(
                "llm.rate_limited attempt=%d/%d backing off %.1fs",
                attempt, MAX_RATELIMIT_RETRIES, delay,
            )
            await asyncio.sleep(delay)
            delay *= 2

    raise LLMUnavailable(f"Rate limited after {MAX_RATELIMIT_RETRIES} attempts: {last}")
