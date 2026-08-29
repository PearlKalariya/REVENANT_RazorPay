"""PII redaction for merchant-facing responses.

Real Razorpay webhooks carry customer email and phone, and those payloads are
stored verbatim as evidence. That is correct for verification and wrong for
display: a recovery dashboard is shown in meetings, screen-shared, and recorded.

Redaction happens at the API boundary rather than at storage, because the raw
payload is what proves an outcome actually happened. Verification needs the
original; humans do not.
"""

from __future__ import annotations

import re

EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+)")
PHONE_RE = re.compile(r"(\+?\d{2})\d{4,}(\d{2})")

#: Keys whose values are redacted wherever they appear in a nested payload.
SENSITIVE_KEYS = frozenset({
    "email", "contact", "phone", "customer_email", "customer_contact",
    "card", "card_id", "token_iin", "vpa", "bank_account",
})


def mask_email(value: str) -> str:
    """a@b.com -> a***@b.com — enough to recognise, not enough to contact."""
    return EMAIL_RE.sub(r"\1***\2", value)


def mask_phone(value: str) -> str:
    """+919812345678 -> +91******78"""
    return PHONE_RE.sub(r"\1******\2", value)


def mask_text(value: str) -> str:
    return mask_phone(mask_email(value))


def redact(obj):
    """Recursively redact PII from any JSON-shaped structure."""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if key.lower() in SENSITIVE_KEYS:
                out[key] = "[redacted]" if not isinstance(value, str) else (
                    mask_text(value))
            else:
                out[key] = redact(value)
        return out
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return mask_text(obj)
    return obj
