# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Two independent processes sharing one Postgres database:

| Component | What it is | Entry point |
|---|---|---|
| `gps_gateway/` | asyncio **TCP** server decoding GT06 tracker frames | `python -m gps_gateway` |
| `api/` | **FastAPI** REST service for the dashboard | `python -m api` (port 55920) |

They are deliberately separate processes: a reconnect storm from devices must
not starve the customer-facing API, and either can be redeployed or scaled
independently. Both write to the same `device_locations` table — the API
never talks to the gateway directly, only through Postgres.

A GT06 tracker cannot speak HTTP; it opens a raw TCP socket and streams binary
frames forever (login → location → heartbeat, each ACKed with an echoed
serial and a correct CRC16-ITU or real hardware retries the packet). The
API's `POST /api/v1/ingest/location` is a second, independent write path for
clients that *do* speak HTTP (phone apps, partner integrations) — both paths
write identical rows, so every read endpoint covers devices from either side.

The companion Angular dashboard lives in the sibling repo
`../gps-tracking-web` and is the only consumer of this API.

## Commands

```bash
# Tests -- plain unittest, no pytest/httpx. 96+ tests, no third-party deps.
python -m unittest discover -s tests -t .
python -m unittest tests.test_users                        # one module
python -m unittest tests.test_users.TestLogin.test_logout_makes_the_token_unusable  # one test

# Lint (configured in pyproject.toml, line-length 100; not installed by default)
ruff check .

# Run against Postgres (needs PG_DSN pointed at a real database)
export PG_DSN="postgresql://postgres:postgres@localhost:5432/trackit"
python3 -m gps_gateway                                       # TCP gateway, :5023
INGEST_API_KEY=dev python3 -m uvicorn api.main:app --port 55920

# Run with no database and no hardware
GATEWAY_SINK=log python -m gps_gateway                        # prints decoded events instead
python tools/simulate_device.py --pings 5                     # fake tracker, real CRCs
API_BACKEND=memory INGEST_API_KEY=dev uvicorn api.main:app --port 55920

# Always use `python -m api`, not the bare uvicorn CLI, when it matters --
# it reads host/port from ApiConfig so the startup banner matches what's bound.

# Export the OpenAPI spec without starting the server
python3 tools/export_openapi.py [--format yaml]

# Generate a VAPID key pair for Web Push
python -m api.push genkey
```

IDE run configs exist for both IntelliJ (`.idea/runConfigurations/`) and VS
Code (`.vscode/launch.json`) — "All services" starts the gateway and API
together with `PG_DSN` already set.

## Architecture

**Repository pattern, everywhere.** Every table-touching class is a
`typing.Protocol` with two implementations: `InMemory*` (tests, and
`API_BACKEND=memory` local dev) and `Postgres*` (production). See
`api/repository.py` (`LocationRepository`) and `api/users_repository.py`
(`UserRepository`). Adding a method means adding it to the Protocol and both
implementations, or the in-memory tests silently stop exercising the real
Postgres SQL.

**Two writers into one table, so shared derived state lives in the database,
not in application code.** `device_locations` is written by both the API's
ingest endpoint and the gateway's `PostgresSink` — independent processes that
must never be allowed to drift from each other. Anything that must stay
consistent across both writers is therefore computed by Postgres itself:
- `notify_vehicle_motion()` (a trigger in `sql/schema.sql`) detects a
  device crossing from stopped to moving or back, and fires `pg_notify`.
  Neither writer knows this happens.
- If you add another cross-writer concern (e.g. a devices registry with
  `last_seen`/`fix_count`), follow the same pattern — a trigger, not a method
  each sink implementation has to remember to call.

**Auth: opaque bearer tokens, not JWT.** `POST /auth/login` returns a random
token (`api/security.py`); the server stores its SHA-256 fingerprint and
looks it up on every request (`api/deps.py: current_user`), re-checking
`is_active` via a join each time. This is deliberate: deactivating an account
must take effect immediately, which a self-contained JWT cannot do without a
revocation list. Passwords are bcrypt.

**Device subscriptions are billing state on the device, not the account.**
`device_subscriptions` (`repository.py`) is keyed by `device_id`, same as
`device_locations` and `user_devices`, and follows the same no-row-means-
unmetered rule those tables use: a device with no row never expires. Once
`subscription_end_date` passes, `latest_for_device`/`history_for_device`
(and the `summary`/`fixes_over_time` aggregates) hide that device's position
data from every viewer — admins included, since this is a billing gate, not
a permission scope. `list_devices` keeps listing it, with
`subscription_status: "expired"`, so it stays discoverable enough to renew via
`PUT /devices/{id}/subscription`. Every change to `subscription_end_date` is
appended to `device_subscription_history` rather than overwritten in place;
omitting it in the `PUT` body defaults to `DEFAULT_SUBSCRIPTION_TERM` (365
days) from today (`routers/devices.py`).

The same row also carries `installed_at` and `sim_expiry_date` — both purely
informational, never gating anything — set through the same endpoint but
with the opposite null-handling from `subscription_end_date`: it is a
renewal field (omitted → reset to the default term), they are edit fields
(omitted → left exactly as stored). `repository.py`'s Postgres
`ON CONFLICT ... DO UPDATE` expresses this with `COALESCE(EXCLUDED.x,
device_subscriptions.x)` for those two columns only. `clear_device_subscription`
clears `subscription_end_date` with an `UPDATE`, not a `DELETE`, so the
other two dates survive lifting metering.

**Per-account device scoping is enforced in the routers, not just the
frontend.** `AuthenticatedUser.devices` (empty for an admin's unlimited
access, otherwise the assigned list) is applied in every router that touches
device data — `api/routers/devices.py`, `stats.py`, and the push fan-out in
`users_repository.py: subscriptions_for_device`. The rule everywhere: a
scoped account gets the exact same response for "not yours" as for "does not
exist" (404, not 403) — the caller must not be able to distinguish the two.
When adding a new device-scoped endpoint, follow this shape, not a 403.

**Web Push runs as a background task inside the API process, driven by
Postgres `LISTEN`/`NOTIFY`.** `api/push.py: run_motion_listener` holds one
dedicated connection (separate from the request pool — `LISTEN` state lives
on the connection) and turns each `vehicle_motion` notification into pushes
via `pywebpush`, scoped to exactly the subscriptions that can see that
device. Started from `api/main.py`'s `lifespan`, so **it never runs on
Vercel** (serverless skips lifespan and is short-lived) — subscribing and
unsubscribing still work anywhere, only delivery needs a long-running host.
`python -m api.push genkey` mints the `VAPID_PUBLIC_KEY`/`VAPID_PRIVATE_KEY`
pair `api/config.py` reads.

**Serverless-safe startup.** Vercel does not reliably run ASGI lifespan
events, so the connection pool is built lazily on first request
(`api/state.py`), not in a startup hook — otherwise every request would 503
on a platform that skips lifespan. `tests/test_serverless.py` exercises this
directly. `vercel_entry.py` sits at the repo root, not inside `api/`,
because Vercel turns every file under a top-level `api/` directory into its
own function — putting the entry point inside would publish `config.py` and
`repository.py` as endpoints.

**Structured logging with request correlation.** Every request gets an id
(`api/middleware.py`) that appears in the response's `X-Request-ID` header,
in the error body, and on every log line for that request — including
application-code logs, via a formatter installed in `python -m api`'s
startup. Log levels are tuned so the stream stays readable: `/health` is
DEBUG (load balancers poll constantly), 4xx is WARNING, 5xx is ERROR with a
traceback, but the client only ever sees `{"detail": ..., "request_id": ...}`
— internal detail never reaches the response.

**Schema is one hand-applied, idempotent file — no migration tool.**
`sql/schema.sql` uses `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT
EXISTS` throughout and is safe to re-run. Nothing applies it automatically;
run it by hand after pulling a schema change:
```bash
docker exec -i postgres-local psql -U postgres -d trackit -v ON_ERROR_STOP=1 < sql/schema.sql
```

**Test harness has no third-party dependencies.** `tests/asgi_client.py` is a
hand-rolled ASGI client (not `httpx`/`TestClient`) that drives the lifespan
protocol itself — get/post/patch helpers, `raise_server_exceptions=False`
semantics. Tests build an app via `create_app(ApiConfig(backend="memory",
...))`; a test needing an authenticated call must log in first and pass
`Authorization: Bearer <token>` (see `ADMIN`/`self.auth()` helpers in
`tests/test_api.py`, `test_stats.py`, `test_users.py`).

## Known, accepted gaps (see README.md "Known gaps" for the full list)

- **The gateway trusts the IMEI a device claims at login** — no allowlist yet.
  Anyone who can reach the TCP port can impersonate a device.
- **GT06 protocol variants** are unverified beyond the mainstream bit layout;
  capture real hardware with `tcpdump` before trusting a new device model.
- **The gateway ACKs before it writes** (`gps_gateway/sinks/batching.py`):
  fixes are queued, ACKed, and written as one `COPY` per batch at least every
  `GATEWAY_FLUSH_INTERVAL` (10s). A crash loses up to that window, and the
  live map can lag by it. Waiting for the write before ACKing is not an
  option at that interval — GT06 hardware resends unACKed packets, which
  would duplicate rows.
- **Exactly one flusher, on purpose.** `notify_vehicle_motion` compares each
  row with the device's previous one, so rows must land in arrival order;
  scale with more gateway processes, not parallel flushers. Multi-row writes
  also mean the trigger may only look at *strictly earlier* rows
  (`(received_at, id) < (NEW.received_at, NEW.id)`): row triggers fire at the
  end of the statement, when later rows of the same batch are already
  visible.
