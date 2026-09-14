"""
FastAPI application.

    python -m api

Read endpoints serve the dashboard; the ingest endpoint accepts positions
from HTTP-capable clients. The TCP gateway (`python -m gps_gateway`) runs as
a separate process and writes to the same table -- keeping them apart means a
burst of device traffic cannot starve the customer-facing API, and either can
be restarted or scaled on its own.
"""

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import ApiConfig
from .errors import register_error_handlers
from .middleware import RequestLoggingMiddleware
from .live import run_fix_listener
from .push import run_motion_listener
from .routers import devices, health, ingest, live, push, stats, users
from .state import close_repository, ensure_repository, init_state

log = logging.getLogger(__name__)

API_PREFIX = "/api/v1"

API_DESCRIPTION = """
Query and ingest GPS device locations.

### Two ways a position arrives

**GT06 trackers speak raw TCP, not HTTP.** Cheap 2G/4G trackers open a socket
to the ingestion gateway on port **5023** and stream binary frames. They can
never call this API — nothing here accepts their protocol.

**This API's ingest endpoint** is for clients that do speak HTTP: a phone app,
a partner integration, or the rare tracker with an HTTP mode.

Both write the same rows, so the read endpoints below cover devices on either
path.

### Authentication

Devices, stats and account endpoints require `Authorization: Bearer <token>`
from `POST /auth/login` — use **Authorize** above. A non-admin account only
ever sees the devices assigned to it: `/devices` lists just those, and
`/devices/{id}/...` for anything else answers exactly as it would for an
unknown id, so a scoped account cannot tell "not yours" from "does not
exist". `POST /ingest/location` is separate — it requires the `X-API-Key`
header instead, and returns `503` until the server sets `INGEST_API_KEY`, so
it can never be left open by accident.
"""

TAGS_METADATA = [
    {"name": "health", "description": "Liveness and database reachability."},
    {"name": "devices", "description": "Read where devices are and where they have been."},
    {"name": "stats", "description": "Aggregates for dashboards: counts and ingestion volume."},
    {
        "name": "ingest",
        "description": (
            "Accept positions over HTTP. Not the path GT06 hardware uses — "
            "those devices talk to the TCP gateway on port 5023."
        ),
    },
    {
        "name": "accounts",
        "description": (
            "Dashboard sign-in and account management. Trackers never "
            "authenticate this way — a GT06 identifies itself by IMEI."
        ),
    },
    {
        "name": "push",
        "description": "Web Push subscriptions for vehicle start/stop notifications.",
    },
    {
        "name": "live",
        "description": "WebSocket: every new fix, pushed the instant it lands.",
    },
]

# Listed separately in the banner footer, or never useful to a caller.
_INTERNAL_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def log_startup_urls(app: FastAPI, config: ApiConfig) -> None:
    """
    Print every endpoint as a clickable URL at startup.

    The host and port come from ApiConfig, so `python -m api` always reports
    the address it is really bound to. Launching through the `uvicorn` CLI
    with a different --port would print the configured one instead; set
    API_PORT to match, or use `python -m api`.
    """
    display_host = "127.0.0.1" if config.host in ("0.0.0.0", "::") else config.host
    base = f"http://{display_host}:{config.port}"

    routes = []
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods or path in _INTERNAL_PATHS:
            continue
        for method in sorted(methods - {"HEAD"}):
            routes.append((method, path))
    routes.sort(key=lambda r: (r[1], r[0]))

    width = 6  # fits "SCHEMA", the longest label in the banner
    log.info("=" * 72)
    log.info("GPS Tracking API ready at %s", base)
    log.info(
        "  backend=%s  ingest=%s",
        config.backend,
        "enabled" if config.ingest_api_key else "disabled (set INGEST_API_KEY)",
    )
    log.info("-" * 72)
    for method, path in routes:
        log.info("  %-*s %s%s", width, method, base, path)
    log.info("-" * 72)
    log.info("  %-*s %s/docs", width, "DOCS", base)
    log.info("  %-*s %s/openapi.json", width, "SCHEMA", base)
    log.info("=" * 72)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Connect eagerly where the platform runs lifespan, so a bad DSN fails at
    boot instead of on the first request. Serverless hosts that skip lifespan
    (Vercel among them) fall back to lazy creation in `ensure_repository`.
    """
    config: ApiConfig = app.state.config
    try:
        await ensure_repository(app)
    except Exception:
        log.exception("Startup could not reach storage; will retry per request")
    log_startup_urls(app, config)

    # Both listeners need a real, persistent Postgres connection of their
    # own -- pointless with the in-memory backend, and only running where
    # lifespan actually executes (never on Vercel; see push.py's and
    # live.py's module docstrings for why).
    listener_tasks: list[asyncio.Task] = []
    if config.backend != "memory" and app.state.users is not None:
        listener_tasks.append(
            asyncio.create_task(run_fix_listener(config, app.state.live_connections))
        )
        if config.vapid_public_key and config.vapid_private_key:
            listener_tasks.append(asyncio.create_task(run_motion_listener(config, app.state.users)))

    try:
        yield
    finally:
        for task in listener_tasks:
            task.cancel()
        for task in listener_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await close_repository(app)


def create_app(config: ApiConfig | None = None) -> FastAPI:
    config = config or ApiConfig.from_env()
    app = FastAPI(
        title="GPS Tracking API",
        version="0.2.0",
        summary="Query device locations ingested by the GT06 gateway.",
        description=API_DESCRIPTION,
        openapi_tags=TAGS_METADATA,
        lifespan=lifespan,
        contact={"name": "Source", "url": "https://github.com/"},
        license_info={"name": "Proprietary"},
    )
    app.state.config = config
    init_state(app)

    # Added last so it wraps every other middleware, and therefore times and
    # logs the whole request including CORS handling.
    app.add_middleware(RequestLoggingMiddleware, slow_request_ms=config.slow_request_ms)
    register_error_handlers(app)

    if config.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.cors_origins,
            # PATCH is needed by the account screens; without it the browser's
            # preflight fails and editing a user looks like a network error.
            allow_methods=["GET", "POST", "PATCH"],
            allow_headers=["*"],
        )

    app.include_router(health.router, prefix=API_PREFIX)
    app.include_router(devices.router, prefix=API_PREFIX)
    app.include_router(stats.router, prefix=API_PREFIX)
    app.include_router(ingest.router, prefix=API_PREFIX)
    app.include_router(users.router, prefix=API_PREFIX)
    app.include_router(push.router, prefix=API_PREFIX)
    app.include_router(live.router, prefix=API_PREFIX)
    return app


app = create_app()
