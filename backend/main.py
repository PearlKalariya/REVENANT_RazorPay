"""REVENANT API.

Autonomous revenue recovery. AI decides, policy controls, Razorpay executes,
events verify, audit proves.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import db
from .agents.llm import resolve_model_name
from .config import get_settings
from .routes_api import router as api_router
from .routes_webhooks import router as webhooks_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    yield
    await db.disconnect()


_settings = get_settings()

app = FastAPI(
    title="REVENANT",
    description="Autonomous revenue recovery agent. Razorpay test mode only.",
    version="0.1.0",
    lifespan=lifespan,
    # Not advertised publicly: the schema would disclose the dev surface.
    docs_url="/docs" if _settings.enable_api_docs else None,
    redoc_url="/redoc" if _settings.enable_api_docs else None,
    openapi_url="/openapi.json" if _settings.enable_api_docs else None,
)


app.include_router(webhooks_router)
app.include_router(api_router)


@app.get("/health")
async def health() -> dict:
    """Liveness. Cheap, no dependencies touched."""
    return {"status": "ok", "service": "revenant"}


@app.get("/health/deep")
async def health_deep() -> dict:
    """Readiness. Touches dependencies.

    Reports presence and mode only — never credential values (spec §12).
    """
    settings = get_settings()
    db_ok = await db.ping()

    return {
        "status": "ok" if db_ok else "degraded",
        "backend": "ok",
        "database": "ok" if db_ok else "unreachable",
        # No "redis" key — Postgres carries the queue by decision D5.
        "razorpay": {
            "mode": settings.razorpay_mode,
            "configured": settings.razorpay_configured,
        },
        # Reports the ACTIVE provider. This hardcoded "anthropic" after the
        # D4 revision to Gemini, so health reported a provider the system was
        # not using — exactly the kind of false-green a health check exists to
        # prevent.
        "llm": {
            "provider": settings.llm_provider,
            "model": resolve_model_name(settings),
            "configured": settings.llm_configured,
        },
        "dev_endpoints": settings.enable_dev_endpoints,
    }
