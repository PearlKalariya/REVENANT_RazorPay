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
import re

from pydantic import BaseModel, ValidationError

from ..config import Settings

log = logging.getLogger(__name__)

DEFAULT_MODELS = {
    "google": "gemini-3.5-flash",
    "anthropic": "claude-sonnet-5",
}

#: Free-tier quotas are PER MODEL PER DAY, and they are small — gemini-2.5-flash
#: allows 20 requests/day, which one agent run can consume. A daily quota cannot
#: be waited out, so exhausting it must not end the run: fall through to the
#: next model, which has its own separate quota.
#:
#: Ordered strongest first. Degrading model quality is bad; a demo that dies
#: mid-run is worse, and the Policy Engine constrains what any of them can cause
#: regardless of which one answered.
#: "gemini-flash-latest" is deliberately EXCLUDED. It returned empty content
#: with finishReason=MAX_TOKENS on a trivial probe, and failed mid-agent with
#: "Requests ending with a model turn are not supported". A moving alias is
#: also the wrong thing to depend on for reproducibility.
#: Only models VERIFIED to work with the full agent flow (tool calling plus
#: LangChain's structured-output prefill) are listed.
#:
#: The gemini-3.x flash models are THINKING models: they spend output tokens on
#: internal reasoning before emitting any text. With a small max_output_tokens
#: they return empty content and finishReason=MAX_TOKENS, which looks exactly
#: like a broken model. It is not — it is an insufficient budget. Hence
#: MAX_OUTPUT_TOKENS below, which is what makes them usable here.
#:
#: Excluded: "*-flash-lite" reject the request outright with "does not support
#: model prefilling", which is how LangChain obtains structured output. They
#: answer plain prompts fine but cannot run this agent. A fallback model that
#: fails differently is not a fallback.
FALLBACK_MODELS = {
    "google": [
        "gemini-3.5-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-2.5-flash",
    ],
    "anthropic": ["claude-sonnet-5", "claude-haiku-4-5-20251001"],
}

#: Thinking models consume this budget on internal reasoning before producing
#: any visible output. Too small and they return nothing at all.
MAX_OUTPUT_TOKENS = 8192

MAX_SCHEMA_RETRIES = 3
MAX_RATELIMIT_RETRIES = 5
#: Free-tier quotas are per-minute, so a usable ceiling has to exceed 60s.
#: An earlier version capped at 8s while the API was asking for 29s, and the
#: run died with the answer sitting in the error message.
MAX_BACKOFF_SECONDS = 75.0


class LLMUnavailable(RuntimeError):
    """No usable model, or every attempt failed."""


def resolve_model_name(settings: Settings) -> str:
    if settings.llm_model:
        return settings.llm_model
    return DEFAULT_MODELS.get(settings.llm_provider, DEFAULT_MODELS["google"])


def model_chain(settings: Settings) -> list[str]:
    """Models to try, in order. An explicit LLM_MODEL disables fallback —
    if the operator pinned a model, silently using a different one would be
    worse than failing."""
    if settings.llm_model:
        return [settings.llm_model]
    return list(FALLBACK_MODELS.get(settings.llm_provider, [resolve_model_name(settings)]))


def is_model_unavailable(exc: Exception) -> bool:
    """Model exists in the catalogue but this key cannot call it.

    Model availability varies by project: gemini-2.5-flash was listed for one
    key and returned 404 on generateContent for another. Treated like quota
    exhaustion — move on rather than failing the run.
    """
    text = str(exc)
    return "404" in text and (
        "NOT_FOUND" in text or "not found" in text.lower()
    )


def is_transient_server_error(exc: Exception) -> bool:
    """Provider-side overload (503 UNAVAILABLE, 500). Not our fault and not a
    quota problem — the model is simply busy.

    Worth a short retry, then a fallback: a demo should not die because one
    model was momentarily oversubscribed.
    """
    text = str(exc)
    return (
        "503" in text
        or "UNAVAILABLE" in text
        or "high demand" in text.lower()
        or "overloaded" in text.lower()
        or "500 INTERNAL" in text
    )


def is_quota_exhausted(exc: Exception) -> bool:
    """A DAILY quota, as opposed to a per-minute rate limit.

    Distinguishing them matters: a per-minute limit is worth waiting out, a
    daily one is not — waiting 60s for a quota that resets tomorrow just
    burns the demo.
    """
    text = str(exc)
    return "PerDay" in text or "per day" in text.lower()


def build_model(settings: Settings, *, temperature: float = 0.0,
                model_name: str | None = None):
    """Build the chat model for the configured provider."""
    if not settings.llm_configured:
        raise LLMUnavailable(
            f"No API key for LLM_PROVIDER={settings.llm_provider!r}. "
            "Set GOOGLE_API_KEY (free: aistudio.google.com/apikey) "
            "or switch LLM_PROVIDER."
        )

    model_name = model_name or resolve_model_name(settings)

    if settings.llm_provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=model_name,
            google_api_key=settings.google_api_key,
            temperature=temperature,
            max_output_tokens=MAX_OUTPUT_TOKENS,
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
    return (
        "429" in text
        or "rate limit" in text
        or "resource_exhausted" in text
        or "quota" in text
    )


def _retry_after_seconds(exc: Exception) -> float | None:
    """Extract the delay the provider itself asked for.

    Gemini 429s carry `'retryDelay': '29s'`. Guessing a backoff when the API
    has told you the answer is how a run dies with the fix in the error text.
    """
    text = str(exc)
    for pattern in (
        r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s",
        r"[Rr]etry in (\d+(?:\.\d+)?)s",
        r"retry-after['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)",
    ):
        m = re.search(pattern, text)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None


async def invoke_with_validation(
    agent_or_factory,
    messages: list[dict],
    schema: type[BaseModel],
    *,
    models: list[str] | None = None,
) -> tuple[BaseModel, dict]:
    """Invoke an agent and guarantee a schema-valid result, or raise.

    `agent_or_factory` may be a built agent, or a callable taking a model name
    so the caller can be rebuilt against a fallback model when a daily quota is
    exhausted.

    Returns (validated_result, state). Never returns a partially parsed object.
    """
    last_error: Exception | None = None
    convo = list(messages)

    for attempt in range(1, MAX_SCHEMA_RETRIES + 1):
        state = await _invoke_with_backoff(
            agent_or_factory, convo, models=models
        )

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


async def _invoke_with_backoff(
    agent_or_factory, messages: list[dict], *, models: list[str] | None = None
) -> dict:
    """Invoke, handling per-minute rate limits and daily quota exhaustion.

    Per-minute limit -> wait it out (the provider tells us how long).
    Daily quota      -> waiting is pointless; move to the next model, which
                        has its own separate quota.
    """
    callable_factory = callable(agent_or_factory) and not hasattr(
        agent_or_factory, "ainvoke"
    )
    chain = list(models or [None]) if callable_factory else [None]
    model_idx = 0

    def current_agent():
        if callable_factory:
            return agent_or_factory(chain[model_idx])
        return agent_or_factory

    delay = 2.0
    last: Exception | None = None

    for attempt in range(1, MAX_RATELIMIT_RETRIES + 1):
        try:
            return await current_agent().ainvoke({"messages": messages})
        except Exception as e:  # noqa: BLE001 - provider SDKs raise varied types
            last = e

            unusable = is_quota_exhausted(e) or is_model_unavailable(e)
            if unusable and callable_factory and model_idx + 1 < len(chain):
                reason = ("daily_quota_exhausted" if is_quota_exhausted(e)
                          else "model_unavailable")
                model_idx += 1
                log.warning("llm.%s falling back to %s", reason, chain[model_idx])
                delay = 2.0
                continue

            # Provider overload: retry this model briefly, then move on rather
            # than spending the whole retry budget on a busy model.
            if is_transient_server_error(e):
                if attempt <= 2:
                    log.warning(
                        "llm.provider_unavailable retrying %s in 5s (attempt %d)",
                        chain[model_idx] or "model", attempt,
                    )
                    await asyncio.sleep(5.0)
                    continue
                if callable_factory and model_idx + 1 < len(chain):
                    model_idx += 1
                    log.warning(
                        "llm.provider_unavailable falling back to %s",
                        chain[model_idx],
                    )
                    continue
                raise

            if not _is_rate_limit(e) or attempt == MAX_RATELIMIT_RETRIES:
                raise

            # Prefer the provider's own instruction over our guess. Pad it
            # slightly: quota windows are not perfectly aligned with our clock.
            asked = _retry_after_seconds(e)
            wait = min(asked + 1.5, MAX_BACKOFF_SECONDS) if asked else min(
                delay, MAX_BACKOFF_SECONDS
            )
            log.warning(
                "llm.rate_limited attempt=%d/%d waiting %.1fs (%s)",
                attempt, MAX_RATELIMIT_RETRIES, wait,
                "provider-specified" if asked else "exponential",
            )
            await asyncio.sleep(wait)
            delay = min(delay * 2, MAX_BACKOFF_SECONDS)

    raise LLMUnavailable(f"Rate limited after {MAX_RATELIMIT_RETRIES} attempts: {last}")


# ---------------------------------------------------------------------------
# Tool-based structured output
# ---------------------------------------------------------------------------
#
# LangChain obtains structured output from Gemini by "prefilling" a model turn.
# Several models reject that outright:
#
#     ValueError: Model 'gemini-3.6-flash' does not support model prefilling.
#     The final request turn must be a user message or a function response.
#
# All the *-flash-lite models and gemini-3.6-flash refuse it, which left the
# fallback chain with almost nothing usable. Depending on a capability that
# varies by model is the wrong foundation.
#
# Function calling, by contrast, is supported everywhere — the agent already
# relies on it for its read-only tools. So the structured result is collected
# the same way: the agent calls a `submit_findings` tool whose arguments ARE
# the schema, and the arguments are validated exactly as before.
#
# Same guarantee, no prefill dependency.

SUBMIT_TOOL_NAME = "submit_findings"


def make_submit_tool(schema: type[BaseModel]):
    """Build the terminal tool an agent calls to deliver its structured result."""
    from langchain_core.tools import StructuredTool

    def _submit(**kwargs) -> str:
        # Validation happens on extraction. Returning a plain acknowledgement
        # keeps the graph terminating cleanly.
        return "Findings recorded."

    return StructuredTool.from_function(
        func=_submit,
        name=SUBMIT_TOOL_NAME,
        description=(
            "Submit your final structured findings. Call this exactly once, as "
            "your last action, after you have gathered enough evidence."
        ),
        args_schema=schema,
    )


def extract_submitted(state: dict, schema: type[BaseModel]) -> BaseModel | None:
    """Pull the schema instance out of the agent's submit_findings call.

    Scans backwards: if the model submitted more than once, the last one is
    its considered answer.
    """
    for message in reversed(state.get("messages", [])):
        for call in getattr(message, "tool_calls", None) or []:
            name = call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
            if name != SUBMIT_TOOL_NAME:
                continue
            args = call.get("args") if isinstance(call, dict) else getattr(call, "args", None)
            if not isinstance(args, dict):
                continue
            try:
                return schema.model_validate(args)
            except ValidationError:
                # Keep looking: an earlier, valid submission is better than none.
                continue
    return None


async def invoke_structured(
    agent_factory,
    messages: list[dict],
    schema: type[BaseModel],
    *,
    models: list[str] | None = None,
) -> tuple[BaseModel, dict]:
    """Run an agent and return a validated structured result, or raise.

    Uses tool-based submission rather than prefill, so it works on every model
    in the fallback chain. Retries on schema failure, feeding the validation
    error back so the model can correct itself.
    """
    last_error: str | None = None
    convo = list(messages)

    for attempt in range(1, MAX_SCHEMA_RETRIES + 1):
        state = await _invoke_with_backoff(agent_factory, convo, models=models)

        result = extract_submitted(state, schema)
        if result is not None:
            return result, state

        last_error = (
            f"No valid {SUBMIT_TOOL_NAME} call found in the response."
            if last_error is None else last_error
        )
        log.warning(
            "llm.no_structured_result attempt=%d/%d", attempt, MAX_SCHEMA_RETRIES
        )
        if attempt < MAX_SCHEMA_RETRIES:
            convo = list(messages) + [
                {
                    "role": "user",
                    "content": (
                        f"You did not submit valid findings. You MUST call the "
                        f"`{SUBMIT_TOOL_NAME}` tool exactly once with every "
                        "required field populated. Ground all figures in tool "
                        "results — do not invent values to satisfy the schema."
                    ),
                }
            ]

    raise LLMUnavailable(
        f"Agent failed to submit valid structured output after "
        f"{MAX_SCHEMA_RETRIES} attempts. {last_error}"
    )
