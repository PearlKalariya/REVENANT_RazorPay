"""Postgres access. asyncpg with plain SQL (decision D12)."""

from __future__ import annotations

import asyncpg

from .config import get_settings

_pool: asyncpg.Pool | None = None


async def connect() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            get_settings().database_url, min_size=1, max_size=10
        )
    return _pool


async def disconnect() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialised.")
    return _pool


async def ping() -> bool:
    """Cheap liveness probe used by /health/deep."""
    try:
        async with pool().acquire() as conn:
            return await conn.fetchval("SELECT 1") == 1
    except Exception:
        return False
