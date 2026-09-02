"""Razorpay test-mode client.

Deliberately narrow. This module is the ONLY place in REVENANT that can move
money, and it exposes exactly one money-moving call: `create_payment_link`.

Capabilities that do not exist here cannot be reached by a hallucinating agent,
a prompt injection, or a bug. There is no refund method, no payout method, and
no generic "call any endpoint" escape hatch — by design (spec §16, D10).

Every request is guarded by `_assert_test_mode()`. Live credentials are refused
at the client boundary, not merely discouraged by configuration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from ..config import Settings

log = logging.getLogger(__name__)

API_BASE = "https://api.razorpay.com/v1"

#: The only currency this integration can settle. Razorpay's Indian entity is
#: INR-only, and every amount in this system is stored in paise. A merchant
#: configured for any other currency must be REFUSED, not silently charged in
#: rupees — the config would say USD while the customer is billed INR.
SUPPORTED_CURRENCIES = frozenset({"INR"})
TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class RazorpayError(Exception):
    """Razorpay rejected the request or was unreachable."""

    def __init__(self, message: str, *, status: int | None = None,
                 retryable: bool = False):
        super().__init__(message)
        self.status = status
        # Distinguishes "safe to retry" from "retrying would double-charge".
        self.retryable = retryable


@dataclass(frozen=True)
class PaymentLink:
    id: str
    short_url: str
    status: str
    amount_minor: int
    reference_id: str


class RazorpayClient:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._assert_test_mode()

    def _assert_test_mode(self) -> None:
        s = self._settings
        if not s.razorpay_configured:
            raise RazorpayError("Razorpay credentials are not configured.")
        if s.razorpay_mode.lower() != "test":
            raise RazorpayError(
                f"RAZORPAY_MODE is {s.razorpay_mode!r}. REVENANT refuses to run "
                "outside test mode."
            )
        if not s.razorpay_key_id.startswith("rzp_test_"):
            raise RazorpayError(
                "Razorpay key id is not a test key. Refusing to proceed."
            )

    @property
    def _auth(self) -> tuple[str, str]:
        return (self._settings.razorpay_key_id, self._settings.razorpay_key_secret)

    async def _request(self, method: str, path: str, **kw) -> dict:
        self._assert_test_mode()
        url = f"{API_BASE}{path}"
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                r = await client.request(method, url, auth=self._auth, **kw)
        except httpx.TimeoutException as e:
            # Retryable ONLY because callers pair retries with an idempotency
            # key. A timeout does not mean the action did not happen.
            raise RazorpayError(
                f"Razorpay request timed out: {path}", retryable=True
            ) from e
        except httpx.HTTPError as e:
            raise RazorpayError(
                f"Razorpay network error: {type(e).__name__}", retryable=True
            ) from e

        if r.status_code >= 500:
            raise RazorpayError(
                f"Razorpay server error {r.status_code}",
                status=r.status_code,
                retryable=True,
            )
        if r.status_code == 429:
            # A rate limit is NOT a bad request. Classifying it with the other
            # 4xx errors marked 15 executions permanently `failed` in a batch
            # run when Razorpay was only asking us to slow down — money that
            # never moved, recorded as a failed recovery.
            raise RazorpayError(
                "Razorpay rate limit (429). Slow down and retry.",
                status=429, retryable=True,
            )
        if r.status_code >= 400:
            detail = ""
            try:
                detail = r.json().get("error", {}).get("description", "")
            except Exception:
                detail = r.text[:200]
            # Other 4xx are our fault: retrying sends the same bad request.
            raise RazorpayError(
                f"Razorpay rejected request ({r.status_code}): {detail}",
                status=r.status_code,
                retryable=False,
            )
        return r.json()

    async def create_payment_link(
        self,
        *,
        amount_minor: int,
        reference_id: str,
        description: str,
        customer_name: str | None = None,
        customer_email: str | None = None,
        customer_contact: str | None = None,
        expire_by: int | None = None,
        currency: str = "INR",
    ) -> PaymentLink:
        """Create a test-mode Payment Link.

        `reference_id` must be unique per link. Razorpay rejects a duplicate,
        which gives a server-side idempotency guarantee on top of our own
        database constraint — two independent defences against double-charging.

        Notifications are hard-disabled. The demo dataset is synthetic and its
        contact details are fabricated; REVENANT must never send mail or SMS to
        them. Re-enabling this requires a human decision.
        """
        if currency not in SUPPORTED_CURRENCIES:
            raise RazorpayError(
                f"Merchant is configured for {currency}, but this integration "
                f"can only settle {sorted(SUPPORTED_CURRENCIES)}. Refusing "
                "rather than charging in the wrong currency."
            )
        if not isinstance(amount_minor, int) or isinstance(amount_minor, bool):
            raise RazorpayError("amount_minor must be an integer.")
        if amount_minor <= 0:
            raise RazorpayError("amount_minor must be positive.")

        payload: dict = {
            "amount": amount_minor,
            "currency": currency,
            "accept_partial": False,
            "description": description[:255],
            "reference_id": reference_id,
            # Hard off. See docstring.
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
        }
        customer = {
            k: v
            for k, v in {
                "name": customer_name,
                "email": customer_email,
                "contact": customer_contact,
            }.items()
            if v
        }
        if customer:
            payload["customer"] = customer
        if expire_by:
            payload["expire_by"] = expire_by

        data = await self._request("POST", "/payment_links", json=payload)
        log.info("payment_link.created id=%s ref=%s", data.get("id"), reference_id)
        return PaymentLink(
            id=data["id"],
            short_url=data["short_url"],
            status=data["status"],
            amount_minor=data["amount"],
            reference_id=data.get("reference_id", reference_id),
        )

    async def find_payment_link_by_reference(self, reference_id: str) -> dict | None:
        """Find a payment link by its reference_id.

        Used to resolve a pending execution: Razorpay refusing a duplicate
        reference_id proves the original call landed, and this recovers the
        link it created rather than guessing at the outcome.
        """
        data = await self._request(
            "GET", "/payment_links", params={"reference_id": reference_id})
        items = data.get("payment_links") or []
        return items[0] if items else None

    async def fetch_payment_link(self, link_id: str) -> dict:
        """Read back a link. Used to verify state from the authoritative
        source rather than trusting our own record (spec §17)."""
        return await self._request("GET", f"/payment_links/{link_id}")
