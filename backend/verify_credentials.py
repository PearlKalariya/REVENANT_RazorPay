"""Credential checker.

Proves each credential actually works by making one read-only API call.
Never prints a credential value — only presence, and whether auth succeeded.

Run:  python -m backend.verify_credentials
"""

from __future__ import annotations

import asyncio

import httpx

from .config import get_settings

OK = "\033[32mPASS\033[0m"
BAD = "\033[31mFAIL\033[0m"
SKIP = "\033[33mSKIP\033[0m"


def _masked(value: str, *, public: bool = False) -> str:
    """Describe a credential without disclosing it.

    Secrets reveal NOTHING but their length — not even a prefix. A prefix is
    enough to narrow a brute force and it ends up in terminal scrollback, CI
    logs, and screen recordings.

    `public=True` is only for non-secret identifiers such as the Razorpay key
    id, where seeing 'rzp_test_' is the point.
    """
    if not value:
        return "(not set)"
    if public:
        return f"{value[:9]}… ({len(value)} chars)"
    return f"(set, {len(value)} chars)"


async def check_razorpay(s) -> bool:
    if not s.razorpay_configured:
        print(f"  {SKIP}  Razorpay — RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not set")
        return False

    if s.razorpay_mode.lower() != "test":
        print(f"  {BAD}  Razorpay — RAZORPAY_MODE is {s.razorpay_mode!r}, must be 'test'")
        return False

    if not s.razorpay_key_id.startswith("rzp_test_"):
        print(
            f"  {BAD}  Razorpay — key id does not start with 'rzp_test_'. "
            "Refusing: this looks like a live key."
        )
        return False

    # Read-only call. Lists at most one payment. Moves no money.
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.get(
                "https://api.razorpay.com/v1/payments",
                params={"count": 1},
                auth=(s.razorpay_key_id, s.razorpay_key_secret),
            )
        except httpx.HTTPError as e:
            print(f"  {BAD}  Razorpay — network error: {type(e).__name__}")
            return False

    if r.status_code == 200:
        count = r.json().get("count", 0)
        print(f"  {OK}  Razorpay — test-mode auth OK ({count} payment(s) visible)")
        return True
    if r.status_code == 401:
        print(f"  {BAD}  Razorpay — 401 Unauthorized. Key id or secret is wrong.")
        return False
    print(f"  {BAD}  Razorpay — HTTP {r.status_code}")
    return False


async def check_anthropic(s) -> bool:
    if not s.anthropic_api_key:
        print(f"  {SKIP}  Anthropic — ANTHROPIC_API_KEY not set")
        return False

    # A models-list call only proves the key authenticates. It returns 200 on
    # an account with zero credits, which then fails on the first real
    # inference. So we spend one token instead and find out now.
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": s.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-5",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        except httpx.HTTPError as e:
            print(f"  {BAD}  Anthropic — network error: {type(e).__name__}")
            return False

    if r.status_code == 200:
        print(f"  {OK}  Anthropic — inference OK (real completion returned)")
        return True

    detail = ""
    try:
        detail = r.json().get("error", {}).get("message", "")
    except Exception:
        detail = r.text[:160]

    if r.status_code == 401:
        print(f"  {BAD}  Anthropic — 401 Unauthorized. Key is wrong or revoked.")
    elif "credit balance" in detail.lower():
        print(f"  {BAD}  Anthropic — key is VALID but the account has no credits.")
        print( "         Add credits at console.anthropic.com -> Plans & Billing.")
    else:
        print(f"  {BAD}  Anthropic — HTTP {r.status_code}: {detail}")
    return False


def check_webhook_secret(s) -> bool:
    """Cannot be verified against Razorpay by API — it only proves itself when a
    real webhook arrives and the signature matches. Presence check only."""
    if not s.razorpay_webhook_secret:
        print(f"  {SKIP}  Webhook secret — not set (needed for signature verification)")
        return False
    print(
        f"  {OK}  Webhook secret — present. NOTE: correctness is only proven "
        "when a real signed webhook verifies."
    )
    return True


async def main() -> None:
    s = get_settings()
    print("\n  REVENANT credential check")
    print("  " + "-" * 56)
    print(f"  RAZORPAY_KEY_ID         {_masked(s.razorpay_key_id, public=True)}")
    print(f"  RAZORPAY_KEY_SECRET     {_masked(s.razorpay_key_secret)}")
    print(f"  RAZORPAY_WEBHOOK_SECRET {_masked(s.razorpay_webhook_secret)}")
    print(f"  ANTHROPIC_API_KEY       {_masked(s.anthropic_api_key)}")
    print(f"  RAZORPAY_MODE           {s.razorpay_mode}")
    print("  " + "-" * 56)

    results = [
        await check_razorpay(s),
        check_webhook_secret(s),
        await check_anthropic(s),
    ]
    print("  " + "-" * 56)
    print(f"  {sum(results)}/3 ready\n")


if __name__ == "__main__":
    asyncio.run(main())
