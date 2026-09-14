"""
Repository lifecycle.

Kept out of main.py so the dependency layer can reach it without importing
the app module, and lazy so the API works on platforms that never run ASGI
lifespan events. Vercel's Python runtime is one of those: a startup hook is
not guaranteed to fire, and a repository built only in `lifespan` would leave
every request answering 503.
"""

import asyncio
import logging

from fastapi import FastAPI, HTTPException, status

from .config import ApiConfig
from .live import ConnectionManager
from .repository import (
    InMemoryLocationRepository,
    LocationRepository,
    PostgresLocationRepository,
)
from .users_repository import (
    InMemoryUserRepository,
    PostgresUserRepository,
    UserRepository,
)

log = logging.getLogger(__name__)


async def _build(config: ApiConfig):
    """
    Returns (repository, users, pool). The pool is None for the memory
    backend, and both repositories share it: pool sizing is tuned for one
    pool per instance, so a second would double a serverless deployment's
    connection count against the database's limit.
    """
    if config.backend == "memory":
        log.warning("API running with the in-memory backend; data is not persisted")
        return InMemoryLocationRepository(), InMemoryUserRepository(), None

    import asyncpg

    pool = await asyncpg.create_pool(
        dsn=config.pg_dsn,
        min_size=config.pg_pool_min,
        max_size=config.pg_pool_max,
        statement_cache_size=config.pg_statement_cache_size,
    )
    log.info(
        "API connected to Postgres (pool %d-%d, statement_cache=%d)",
        config.pg_pool_min,
        config.pg_pool_max,
        config.pg_statement_cache_size,
    )
    return PostgresLocationRepository(pool), PostgresUserRepository(pool), pool


async def ensure_repository(app: FastAPI) -> LocationRepository:
    """
    Return the repository, building it on first use.

    Safe to call concurrently: the lock stops a burst of cold-start requests
    from each opening their own connection pool.
    """
    repo = getattr(app.state, "repository", None)
    if repo is not None:
        return repo

    async with app.state.repository_lock:
        repo = getattr(app.state, "repository", None)
        if repo is not None:
            return repo
        try:
            repo, users, pool = await _build(app.state.config)
        except Exception:
            log.exception("Could not reach storage")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Storage unavailable",
            ) from None
        app.state.repository = repo
        app.state.users = users
        app.state.pool = pool
        await _bootstrap_admin(app.state.config, users)
        return repo


async def ensure_users(app: FastAPI) -> UserRepository:
    """The account store, built alongside the location repository."""
    await ensure_repository(app)
    return app.state.users


async def _bootstrap_admin(config: ApiConfig, users: UserRepository) -> None:
    """
    Seed the first admin so a fresh database is reachable at all.

    Only ever fires when the table is empty. Without it a new deployment has
    no way in: every account-creating endpoint requires an admin to call it.
    """
    if not (config.bootstrap_admin_username and config.bootstrap_admin_password):
        return
    try:
        created = await users.ensure_bootstrap_admin(
            config.bootstrap_admin_username, config.bootstrap_admin_password
        )
    except Exception:
        # A failure here must not take the API down; the rest of it is
        # readable without accounts.
        log.exception("Could not create the bootstrap admin")
        return
    if created:
        log.warning(
            "Created bootstrap admin '%s'. Change its password and clear "
            "BOOTSTRAP_ADMIN_PASSWORD.",
            config.bootstrap_admin_username,
        )


async def close_repository(app: FastAPI) -> None:
    pool = getattr(app.state, "pool", None)
    app.state.repository = None
    app.state.users = None
    app.state.pool = None
    if pool is not None:
        await pool.close()


def init_state(app: FastAPI) -> None:
    app.state.repository = None
    app.state.users = None
    app.state.pool = None
    # Created here rather than in lifespan, which may never run.
    app.state.repository_lock = asyncio.Lock()
    # No I/O of its own -- safe to build eagerly regardless of backend or
    # whether lifespan ever runs. Sockets register with it directly; only
    # *delivering* a fix to them needs the LISTEN task started in lifespan.
    app.state.live_connections = ConnectionManager()
