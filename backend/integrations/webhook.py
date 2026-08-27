"""Razorpay webhook signature verification.

Razorpay signs each webhook with HMAC-SHA256 over the RAW request body, keyed
by the webhook secret, and sends the hex digest in the X-Razorpay-Signature
header.

Two rules that matter more than they look:

1. **Verify against raw bytes, never re-serialised JSON.** `json.dumps(json.
   loads(body))` will not reproduce the original byte sequence — key order and
   whitespace shift — and the HMAC will not match. The raw body must be carried
   from the request to here untouched.

2. **Compare in constant time.** A plain `==` on digests leaks how many leading
   characters matched, which is enough to forge a signature byte by byte given
   enough attempts. `hmac.compare_digest` does not leak that.

This module is pure: no I/O, no framework types, no globals. That makes it
fully testable without credentials or a network.
"""

from __future__ import annotations

import hashlib
import hmac


class SignatureError(Exception):
    """Raised when a webhook signature cannot be trusted."""


def compute_signature(raw_body: bytes, secret: str) -> str:
    """Compute the expected hex signature for a raw webhook body.

    Also used by the replay harness to SIGN payloads, so replayed events go
    through the exact same verification path as real ones (decision D3).
    The harness does not bypass verification — it satisfies it.
    """
    if not secret:
        raise SignatureError("Webhook secret is not configured.")
    return hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()


def verify_signature(raw_body: bytes, signature: str | None, secret: str) -> bool:
    """Return True only if `signature` is a valid Razorpay signature.

    Fails closed on every unhappy path: missing header, empty secret, wrong
    type, malformed digest. Never raises for a merely-invalid signature —
    an invalid signature is a False, not an exception, so callers cannot
    accidentally swallow it in a try/except and continue.
    """
    if not signature or not isinstance(signature, str):
        return False
    if not secret:
        return False
    try:
        expected = compute_signature(raw_body, secret)
    except SignatureError:
        return False
    return hmac.compare_digest(expected, signature.strip())
