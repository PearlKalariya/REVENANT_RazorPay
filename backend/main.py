"""REVENANT API.

Autonomous revenue recovery. AI decides, policy controls, Razorpay executes,
events verify, audit proves.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import db
from .config import get_settings
from .routes_webhooks import router as webhooks_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    yield
    await db.disconnect()


app = FastAPI(
    title="REVENANT",
    description="Autonomous revenue recovery agent. Razorpay test mode only.",
    version="0.1.0",
    lifespan=lifespan,
)


app.include_router(webhooks_router)


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
        "llm": {"provider": "anthropic", "configured": bool(settings.anthropic_api_key)},
        "dev_endpoints": settings.enable_dev_endpoints,
    }
