"""Postgres connection pool and per-request RLS context.

Two helpers:
  - admin_conn(): default role (service_role / postgres). RLS bypassed.
                  Use for admin operations, webhook handlers, system tasks.
  - user_conn(user_id): SET LOCAL ROLE authenticated + session variable.
                        RLS enforced. Use for user-facing reads.

Both are async context managers that acquire a connection from the pool.
user_conn wraps in a transaction because SET LOCAL is transaction-scoped.

Connection pool lifecycle is managed via FastAPI lifespan (see main.py).
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional
from uuid import UUID

import asyncpg

log = logging.getLogger("locke.db")

_pool: Optional[asyncpg.Pool] = None


async def init_pool() -> None:
    """Create the global connection pool. Called from app startup."""
    global _pool
    if _pool is not None:
        return

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set")

    # statement_cache_size=0 is required when connecting through Supabase's
    # transaction-mode pooler (port 6543) because prepared statements don't
    # round-trip cleanly across pooled connections.
    use_pooler = ":6543/" in dsn

    _pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=2,
        max_size=10,
        command_timeout=30,
        statement_cache_size=0 if use_pooler else 100,
    )
    log.info("db.pool.created use_pooler=%s", use_pooler)


async def close_pool() -> None:
    """Close the global pool. Called from app shutdown."""
    global _pool
    if _pool is None:
        return
    await _pool.close()
    _pool = None
    log.info("db.pool.closed")


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialized")
    return _pool


@asynccontextmanager
async def admin_conn() -> AsyncIterator[asyncpg.Connection]:
    """Acquire a connection that uses the default role (service_role).

    RLS is bypassed. Authorization is the caller's responsibility.
    Use for admin operations, webhook handlers, migrations.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        yield conn


@asynccontextmanager
async def user_conn(user_id: UUID) -> AsyncIterator[asyncpg.Connection]:
    """Acquire a connection scoped to a specific user.

    Sets ROLE authenticated + app.current_user_id for the transaction.
    RLS is enforced. Use anywhere a request is acting on behalf of a user
    and we want the database itself to filter results to what they can see.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE authenticated")
            # set_config(name, value, is_local) is the SQL function form of
            # SET LOCAL; it accepts placeholders, while bare SET does not.
            await conn.execute(
                "SELECT set_config('app.current_user_id', $1, true)",
                str(user_id),
            )
            yield conn
