"""API security controls.

Two independent controls guard the dev-only surface, because either one alone
has a realistic failure mode:

1. **Loopback binding.** Dev endpoints refuse any request that did not
   originate from 127.0.0.1/::1. A tunnel, a reverse proxy, or a misconfigured
   bind cannot reach them regardless of how the flags are set.
2. **API key.** Mutating endpoints require a shared secret.

The loopback check exists because a feature flag is a configuration mistake
waiting to happen, and this system was in fact exploited through exactly that
mistake: ENABLE_DEV_ENDPOINTS defaulted to true while the app sat behind a
public Cloudflare tunnel, letting an unauthenticated caller inject a forged
"payment succeeded" event that stored as signature_valid = TRUE.

A proxy header such as X-Forwarded-For is deliberately NOT trusted for this
decision — it is attacker-controlled, and trusting it would hand back the
exact bypass this control exists to close.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging

from fastapi import HTTPException, Request, status

from .config import Settings, get_settings

log = logging.getLogger(__name__)

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _client_host(request: Request) -> str:
    return (request.client.host if request.client else "") or ""


def is_loopback(request: Request) -> bool:
    host = _client_host(request)
    if host in LOOPBACK:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_local_dev(request: Request) -> None:
    """Guard for dev-only endpoints. Both conditions must hold."""
    settings = get_settings()

    if not settings.enable_dev_endpoints:
        # 404, not 403: do not confirm the endpoint exists.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")

    if not is_loopback(request):
        log.warning(
            "security.dev_endpoint_blocked path=%s remote=%s",
            request.url.path, _client_host(request) or "<unknown>",
        )
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")


def require_api_key(request: Request) -> None:
    """Guard for mutating endpoints (decision D9).

    Constant-time comparison: a plain == leaks how many leading characters
    matched, which is enough to recover the key byte by byte.
    """
    settings: Settings = get_settings()

    if not settings.api_key:
        # Fail closed. An unset key must never mean "allow everyone".
        log.error("security.api_key_not_configured path=%s", request.url.path)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Server is not configured for authenticated requests.",
        )

    provided = request.headers.get("x-api-key") or ""
    if not hmac.compare_digest(provided, settings.api_key):
        log.warning(
            "security.unauthorized path=%s remote=%s key_present=%s",
            request.url.path, _client_host(request) or "<unknown>", bool(provided),
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing API key")
