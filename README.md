# GPS Tracking Platform

Two processes over one Postgres database:

| Component | What it is | Entry point |
|---|---|---|
| `gps_gateway/` | asyncio **TCP** server decoding GT06 tracker frames | `python -m gps_gateway` |
| `api/` | **FastAPI** REST service for dashboards and HTTP clients | `uvicorn api.main:app` |

They are deliberately separate: a reconnect storm from thousands of devices
must not starve the customer-facing API, and either side can be restarted or
scaled on its own.

```
GT06 tracker ──raw TCP binary──▶ gps_gateway :5023 ─┐
                                                    ├─▶ Postgres ──▶ api :55920 ──▶ dashboard
phone app / HTTP tracker ──JSON POST──▶ api :55920 ──┘
```

## How a device sends its location

**A GT06 tracker cannot POST JSON.** These are cheap 2G/4G modems with no TLS
and no HTTP client. They do exactly one thing: open a raw TCP socket to an IP
and port you configure, then stream binary frames over it and keep the socket
open for hours. That is why the gateway exists and why FastAPI cannot replace
it for real hardware.

The conversation looks like this:

| # | Direction | Frame | Meaning |
|---|---|---|---|
| 1 | device → server | `78 78 0D 01 <8-byte IMEI> <serial> <crc> 0D 0A` | login |
| 2 | server → device | `78 78 05 01 <serial> <crc> 0D 0A` | login ACK |
| 3 | device → server | `78 78 22 12 <date><sats><lat><lon><speed><flags> …` | location |
| 4 | server → device | `78 78 05 12 <serial> <crc> 0D 0A` | location ACK |
| 5 | device → server | `78 78 0A 13 …` every 3–5 min | heartbeat |

Two details matter. The **serial number** in the ACK must echo the serial the
device sent — that is how it pairs an ACK to a packet — and the **CRC16-ITU**
must be correct or real Concox hardware treats the ACK as noise and retries
the packet forever.

**Pointing real hardware at it.** Most GT06 clones take an SMS command like
`SERVER,0,<ip>,<port>,0#` (check your unit's sheet — some use `ADMINIP` or a
Windows config tool). The device needs to reach your IP, so the gateway has
to be on a public address or behind a forwarded port, and the tracker's SIM
needs a data plan. On power-up it connects and starts sending unprompted.

**The HTTP path** (`POST /api/v1/ingest/location`) is for clients that *can*
speak HTTP: a phone app, a partner integration, or one of the few trackers
with an HTTP mode. Both paths write the same rows.

## Quick start (Postgres in Docker)

Assumes a `postgres:16` container published on 5432 — here `postgres-local`,
with the `trackit` database.

```bash
# 1. Create the schema (once). psql runs inside the container, so it is not
#    needed on the host.
docker exec -i postgres-local psql -U postgres -d trackit -v ON_ERROR_STOP=1 < sql/schema.sql

# 2. Point both services at that database.
export PG_DSN="postgresql://postgres:postgres@localhost:5432/trackit"

# 3. TCP gateway (terminal 1).
pip3 install asyncpg
python3 -m gps_gateway

# 4. REST API (terminal 2).
INGEST_API_KEY=dev python3 -m uvicorn api.main:app --port 55920

# 5. Fake tracker (terminal 3).
python3 tools/simulate_device.py --pings 5
```

Confirm rows are landing:

```bash
docker exec postgres-local psql -U postgres -d trackit \
  -c "SELECT id, device_id, latitude, longitude, received_at
      FROM device_locations ORDER BY id DESC LIMIT 5;"
```

If the container is named differently, `docker ps` will show it; substitute
the name and the `-U` / `-d` values from its `POSTGRES_USER` / `POSTGRES_DB`.

### No database, no hardware

```bash
GATEWAY_SINK=log python -m gps_gateway          # prints decoded events
python tools/simulate_device.py --pings 5       # fake tracker, another terminal

API_BACKEND=memory INGEST_API_KEY=dev uvicorn api.main:app --port 55920
```

The simulator sends a login, five locations drifting north from Bengaluru,
and heartbeats — building frames with real CRCs, so it exercises the same
code path as hardware.

## Running from an IDE

Run configurations are committed for both IDEs — pick **All services** to
start the gateway and API together.

| Configuration | What it runs |
|---|---|
| Gateway (Postgres) | `python -m gps_gateway` against `trackit` |
| Gateway (log sink) | same, printing events instead of writing |
| REST API (Postgres) | `python -m api` on :55920 |
| REST API (in-memory) | same, no database needed |
| Simulate device | `tools/simulate_device.py --pings 5` |
| Tests | `unittest discover -s tests -t .` |
| All services | gateway + API together (compound) |

- **IntelliJ / PyCharm** — `.idea/runConfigurations/*.xml`, picked up on next
  project open. They pin the interpreter to
  `/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13`; change
  `SDK_HOME` if yours differs.
- **VS Code** — `.vscode/launch.json`, needs the Python extension.

Both set `PG_DSN` in the configuration itself, so no shell export is needed.

Use `python -m api` rather than the `uvicorn` CLI: it takes host and port from
`ApiConfig`, so the URLs printed at startup are the ones actually bound.

## Startup output

Each service prints its addresses on boot:

```
GPS ingestion gateway ready (GT06 over plain TCP)
  LISTEN  tcp://0.0.0.0:5023
  LOCAL   tcp://127.0.0.1:5023
  sink=postgres  idle_timeout=600.0s  max_buffer=8192B
  point a device here:  SERVER,0,<this-host>,5023,0#
```

```
GPS Tracking API ready at http://127.0.0.1:55920
  backend=postgres  ingest=enabled
  GET    http://127.0.0.1:55920/api/v1/devices
  GET    http://127.0.0.1:55920/api/v1/devices/{device_id}/latest
  GET    http://127.0.0.1:55920/api/v1/devices/{device_id}/locations
  GET    http://127.0.0.1:55920/api/v1/health
  POST   http://127.0.0.1:55920/api/v1/ingest/location
  DOCS   http://127.0.0.1:55920/docs
  SCHEMA http://127.0.0.1:55920/openapi.json
```

The gateway prints `tcp://` deliberately — it is a raw socket listener, not
HTTP, so a browser cannot open it.

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/health` | liveness + database reachability |
| `GET` | `/api/v1/devices` | devices this account can see, with last-seen time |
| `GET` | `/api/v1/devices/{id}/latest` | most recent fix |
| `GET` | `/api/v1/devices/{id}/locations?limit=&since=&until=` | history, newest first |
| `GET` | `/api/v1/stats/summary?active_minutes=&recent_hours=` | headline counts, scoped to this account |
| `GET` | `/api/v1/stats/fixes?hours=&bucket_minutes=` | ingestion volume over time, scoped |
| `POST` | `/api/v1/ingest/location` | HTTP ingest (needs `X-API-Key`) |
| `POST` | `/api/v1/auth/login` | exchange credentials for a bearer token |
| `POST` | `/api/v1/auth/logout` | end the current session |
| `GET` | `/api/v1/auth/me` | the signed-in account |
| `GET` | `/api/v1/users` | list accounts (admin) |
| `POST` | `/api/v1/users` | create an account, optionally with assigned devices (admin) |
| `PATCH` | `/api/v1/users/{id}` | edit, deactivate, reactivate, or reassign devices (admin) |
| `GET` | `/api/v1/devices/{id}/subscription` | a device's current subscription status (admin) |
| `PUT` | `/api/v1/devices/{id}/subscription` | set or renew a device's subscription end date (admin) |
| `DELETE` | `/api/v1/devices/{id}/subscription` | lift metering on a device entirely (admin) |
| `GET` | `/api/v1/devices/{id}/subscription-history` | every past change to that device's subscription end date (admin) |
| `GET` | `/api/v1/push/vapid-public-key` | the key a browser needs to create a push subscription |
| `POST` | `/api/v1/push/subscribe` | register this browser for vehicle start/stop pushes |
| `POST` | `/api/v1/push/unsubscribe` | stop sending this browser pushes |

Every endpoint above `/health` requires `Authorization: Bearer <token>`
except ingest, which uses `X-API-Key` instead — see **Dashboard accounts**.
Ingest returns **503 until `INGEST_API_KEY` is set** — an open ingest
endpoint would let anyone forge a device's position.

## Dashboard accounts

Accounts exist for the web dashboard. Trackers never authenticate this way — a
GT06 identifies itself by IMEI over TCP and has no concept of a user.

`POST /auth/login` returns an opaque token to send back as
`Authorization: Bearer <token>`. Tokens are **stored** (hashed) rather than
self-contained, which is what makes deactivation immediate: switching an
account off drops its sessions, and every later request re-checks the flag on
a join. A self-contained token would keep working until it expired, which is
exactly the wrong behaviour for a "deactivate this user" button.

Passwords are bcrypt hashes; a hash never leaves the database. Deactivating is
a flag, not a delete, so an account stays intelligible in the audit trail and
can be restored in one click. Two edits are refused: an admin cannot
deactivate or demote **themselves**, and the **last active admin** cannot be
removed by anyone — either would leave nobody able to administer the system.

**Non-admin accounts are scoped to assigned devices, enforced here, not just
in the dashboard.** `/devices`, `/devices/{id}/...` and `/stats/*` all filter
by `user_devices` server-side. A device that exists but is not assigned reads
exactly like one that does not exist — 404, never 403 — so a scoped account
cannot even confirm an unassigned device's presence.

Set `BOOTSTRAP_ADMIN_USERNAME` and `BOOTSTRAP_ADMIN_PASSWORD` to seed the first
admin. It only ever fires when the users table is empty, so it cannot
resurrect a deleted admin or silently reset a password — clear the password
variable once you have signed in and changed it.

Run `sql/schema.sql` again to create the account tables; it is idempotent.

## Device subscriptions

Billing is per tracked device, not per dashboard account. Every device has an
optional `subscription_end_date`; **no row means unmetered** — a device is
fully visible until an admin opts it into this at all, so turning the feature
on cannot silently hide an existing fleet.

Once the date passes, `GET /devices/{id}/latest`, `.../locations`, and the
`/stats/*` aggregates hide that device — 404 or an empty result, exactly like
an unknown device — for **every** viewer, admins included: this is a billing
gate, not a permission scope, so being an admin does not bypass it. `GET
/devices` keeps listing the device with `subscription_status: "expired"` so it
stays discoverable, and ingest is never blocked — a lapsed device keeps
accumulating location history instead of losing it while unpaid.

An admin manages this with `PUT /devices/{id}/subscription` (set or renew),
`DELETE /devices/{id}/subscription` (lift metering entirely, back to
unmetered), and `GET /devices/{id}/subscription-history` for the full
renewal trail — every change records the previous date, the new one, which
admin made it, and when, rather than overwriting silently. Omit
`subscription_end_date` in the `PUT` body (or send `{}`) to onboard a device
on the default term — 365 days from today.

The same row also carries two informational dates that never gate anything:
`installed_at` (when the tracker was fitted) and `sim_expiry_date` (when its
cellular SIM/data plan runs out — the software has no way to enforce this,
it's a heads-up for a human). Set either through the same `PUT
/devices/{id}/subscription` call. Unlike `subscription_end_date`, these two
are plain edit fields: omit one and it stays exactly as stored, rather than
being reset — so renewing a subscription never wipes them, and setting one
never touches the other. Clearing the subscription (`DELETE`) only clears
`subscription_end_date`; both dates survive that too. All three ride along
on every `GET /devices` row.

## Web Push

Set `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` (generate a pair with `python -m
api.push genkey`) and the API can push a browser notification when a device
starts or stops moving. Detection happens in Postgres: a trigger on
`device_locations` (`sql/schema.sql`) sees every insert from **both**
writers — this API's ingest endpoint and the TCP gateway — and fires
`pg_notify` on a genuine start/stop transition, so neither writer has to know
push exists. `run_motion_listener` (`api/push.py`) holds one dedicated
`LISTEN` connection and turns each notification into pushes via `pywebpush`,
scoped the same way every other device-facing endpoint is: only to
subscriptions belonging to an account that can see that device.

The listener starts from `lifespan`, so **it does not run on Vercel** —
serverless skips lifespan and is short-lived. Subscribing and unsubscribing
still work anywhere; only delivery needs a long-running host such as the OCI
VM this project already runs the gateway on.

## Logging

Every API call is logged on completion, with a per-request id that also comes
back as the `X-Request-ID` header and in the body of any error response — so
a user reporting a failure hands you the exact string to grep for.

```
11:44:10 INFO    [api.access] [c4208db9745b] 127.0.0.1 POST /api/v1/ingest/location -> 202 in 8.2ms
11:44:10 WARNING [api.error]  [415b94881c60] POST /api/v1/ingest/location -> 422: body.latitude: Input should be less than or equal to 90
11:44:10 WARNING [api.access] [415b94881c60] 127.0.0.1 POST /api/v1/ingest/location -> 422 in 0.7ms
```

Levels are chosen so the log stays readable:

| Situation | Level |
|---|---|
| `GET /health` | DEBUG — load balancers poll it constantly |
| Normal 2xx/3xx | INFO |
| Any 4xx, or slower than `API_SLOW_REQUEST_MS` | WARNING |
| 5xx and unhandled exceptions | ERROR, with traceback |

Errors are logged with their cause: a 422 names the fields that failed
validation, a 4xx logs its detail message, and an unhandled exception logs a
full traceback while the client gets only `{"detail": "Internal server
error", "request_id": "..."}` — internal detail never reaches the response.

`python -m api` installs a formatter carrying `[request_id]` on every line,
including logs from application code, and disables uvicorn's own access log
since `api.access` supersedes it.

## Swagger / OpenAPI

FastAPI serves the docs itself — no extra package, nothing to configure:

| URL | What |
|---|---|
| http://127.0.0.1:55920/docs | **Swagger UI** — interactive, try requests here |
| http://127.0.0.1:55920/redoc | ReDoc — cleaner for reading |
| http://127.0.0.1:55920/openapi.json | raw OpenAPI 3.1 spec |

The ingest endpoint is declared as an `apiKey` security scheme, so Swagger UI
shows an **Authorize** button: click it, paste the value of `INGEST_API_KEY`
(`dev` locally), and `POST /ingest/location` becomes callable from the page.
Without it that request returns 401 and the docs page cannot exercise it.

Every operation carries a summary, a description, request/response examples
and its real error responses (401, 404, 422, 503), so the page is usable as
the API contract rather than just a route list.

Export the spec without starting the server — handy for client codegen, or
for diffing the contract in review:

```bash
python3 tools/export_openapi.py                # -> openapi.json
python3 tools/export_openapi.py --format yaml  # needs PyYAML
```

## Postman

[`postman/`](postman/) holds a collection and a matching environment:

| File | Import as |
|---|---|
| `GPS-Tracking-API.postman_collection.json` | Collection |
| `GPS-Local.postman_environment.json` | Environment — select it after importing |

16 requests across Health, Ingest, Devices, Error cases and Schema. Each one
carries `pm.test` assertions, so **Run collection** gives a pass/fail report
rather than just responses. Variables: `baseUrl`, `apiKey`, `deviceId`.

Start the API first (`INGEST_API_KEY=dev python3 -m api`) — `apiKey` in the
environment defaults to `dev` to match.

The Ingest folder must run before Devices: it creates the rows the device
assertions read back. Running the whole collection top to bottom does this in
the right order.

Note the collection only covers the HTTP API. GT06 trackers never touch it —
they speak binary over TCP to the gateway on :5023, which Postman cannot
send. Use `python3 tools/simulate_device.py` for that side.

## Deployment (Oracle Cloud + Vercel + Supabase)

```
GT06 trackers ──TCP:5023──▶ OCI Always Free VM (gps_gateway) ─┐
                                                              ├─▶ Supabase Postgres
phone apps ──HTTPS──▶ Vercel (api) ───────────────────────────┘
```

The gateway cannot run on Vercel: it needs a raw TCP port and a process that
holds sockets open for hours, neither of which serverless provides. An OCI
Always Free VM gives it both, plus a permanent public IPv4 for the tracker's
SMS command — at no cost, and without expiring when the trial credits do.

Full walkthrough: **[deploy/oci/README.md](deploy/oci/README.md)**.

### 1. Supabase

Run the schema once from the SQL editor, or:

```bash
psql "$SUPABASE_DIRECT_DSN" -f sql/schema.sql
```

Supabase offers two pooled connection strings and they are **not**
interchangeable here:

| Consumer | Port | Why |
|---|---|---|
| OCI gateway | **5432** session pooler | one long-lived process; prepared statements work |
| Vercel API | **6543** transaction pooler | many short-lived instances; needs `PG_STATEMENT_CACHE_SIZE=0` |

asyncpg caches prepared statements, which a transaction-mode pooler cannot
support — a connection goes to a different client between statements. Setting
`PG_STATEMENT_CACHE_SIZE=0` disables that cache. Skipping this produces
`prepared statement "__asyncpg_stmt_x__" does not exist` under load rather
than on the first request, so it is easy to miss in testing.

### 2. Gateway on Oracle Cloud

Create an Ubuntu instance (Ampere A1 if capacity allows), pasting
[`deploy/oci/cloud-init.yaml`](deploy/oci/cloud-init.yaml) as the
initialization script, and reserve its public IP. Then:

```bash
git clone <your-repo> /opt/gps-tracking && cd /opt/gps-tracking
sudo nano /etc/gps-gateway.env     # PG_DSN, session pooler on 5432
sudo docker compose -f deploy/oci/docker-compose.yml --env-file /etc/gps-gateway.env up -d --build
```

**Open port 5023 in both firewalls.** OCI has two, and traffic needs both:
the VCN security list ingress rule *and* the VM's own iptables. Ubuntu images
on OCI REJECT everything except SSH by default, so a correct security list
alone still leaves trackers hanging with nothing in the gateway log. This is
the most common OCI mistake — see the deploy guide for the exact commands.

Verify without hardware:

```bash
python3 tools/simulate_device.py --host <your-public-ip> --port 5023 --pings 3
```

### 3. API on Vercel

```bash
vercel link
vercel env add PG_DSN                  # the 6543 transaction pooler string
vercel env add INGEST_API_KEY          # a real secret, not "dev"
vercel env add PG_STATEMENT_CACHE_SIZE # 0
vercel env add PG_POOL_MIN             # 0
vercel env add PG_POOL_MAX             # 1
vercel env add API_CORS_ORIGINS        # https://your-dashboard.vercel.app
vercel deploy --prod
```

`vercel.json` routes every path to [`vercel_entry.py`](vercel_entry.py), which
sits at the root on purpose: Vercel turns each file under a top-level `api/`
directory into its own function, which would publish `config.py` and
`repository.py` as endpoints.

Pool sizing matters. Each Vercel instance builds its own pool, so `PG_POOL_MAX`
is multiplied by the number of concurrent instances — `0/1` against the pooler
is the safe setting.

### Deployment gotchas already handled

- **Vercel does not reliably run ASGI lifespan.** The database pool is created
  lazily on first request ([api/state.py](api/state.py)) rather than in a
  startup hook, which would otherwise leave every request answering 503.
  Covered by `tests/test_serverless.py`.
- **Restarts drop every device socket.** Trackers reconnect on their own, but
  expect a gap in fixes at each gateway deploy.
- **The gateway trusts the IMEI in the login packet.** On a public IP, anyone
  who finds the port can impersonate a device. Restrict the ingress CIDR if
  you can, and add an IMEI allowlist before this carries anything that
  matters.

## Configuration

| Variable | Default | Applies to |
|---|---|---|
| `GATEWAY_HOST` / `GATEWAY_PORT` | `0.0.0.0` / `5023` | gateway |
| `GATEWAY_SINK` | `postgres` | gateway (`postgres` or `log`) |
| `GATEWAY_IDLE_TIMEOUT` | `600` | gateway, seconds before dropping a silent socket |
| `GATEWAY_MAX_BUFFER` | `8192` | gateway, max unparsed bytes per connection |
| `GATEWAY_BATCH_SIZE` | `500` | gateway, max fixes per database write (one `COPY`); `1` turns batching off and ACKs only after each write |
| `GATEWAY_FLUSH_INTERVAL` | `10` | gateway, seconds a partial batch waits before it is written — devices are ACKed on queueing, so this is also the live-map lag and what a crash can lose |
| `GATEWAY_QUEUE_MAX` | `10000` | gateway, fixes waiting to be written before sessions stop reading their sockets |
| `PG_DSN` | `postgresql://user:pass@localhost:5432/gps` | both |
| `API_HOST` / `API_PORT` | `127.0.0.1` / `55920` | api |
| `API_BACKEND` | `postgres` | api (`postgres` or `memory`) |
| `INGEST_API_KEY` | *(unset — ingest disabled)* | api |
| `API_CORS_ORIGINS` | *(none)* | api, comma-separated |
| `PG_POOL_MIN` / `PG_POOL_MAX` | `1` / `10` api, `1` / `5` gateway | both |
| `PG_STATEMENT_CACHE_SIZE` | `100` | both — set `0` behind a transaction pooler |
| `API_SLOW_REQUEST_MS` | `1000` | api, threshold for a slow-request warning |
| `SESSION_TTL_HOURS` | `12` | api, how long a dashboard sign-in lasts |
| `BOOTSTRAP_ADMIN_USERNAME` | *(unset)* | api, seeds the first admin |
| `BOOTSTRAP_ADMIN_PASSWORD` | *(unset)* | api, only used while no account exists |
| `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` | *(unset — push disabled)* | api, from `python -m api.push genkey` |
| `VAPID_SUBJECT` | `mailto:admin@example.com` | api, contact URL/email sent with every push |

## Schema

DDL lives in [`sql/schema.sql`](sql/schema.sql) and is applied by hand, not by
the running service — so the gateway needs no schema-modifying privileges and
migrations stay reviewable. It is idempotent, and also adds the columns that
older versions of the table lacked.

## Tests

```bash
python -m unittest discover -s tests -t .
```

149 tests. They cover CRC against the published X.25 check value, coordinate
and hemisphere decoding, frame reassembly across TCP boundaries, a full
device conversation over a real socket, the API's auth (including per-account
device scoping on `/devices` and `/stats`), validation and paging, request/
error logging, push subscribe/unsubscribe, and serverless behaviour with no
lifespan events. `pywebpush`/`py-vapid` are needed for the push tests to run
(`pip install -r requirements.txt`); everything else has no third-party test
dependencies.

## Known gaps

1. **Protocol variants** — GT06 clones differ. Bit layouts here follow the
   mainstream spec (bit 10 north, bit 11 west, bit 12 fix valid). Capture your
   actual hardware with `tcpdump` before trusting them.
2. **Extended fields** — mileage, ACC and alarm codes past byte 18 are parsed
   as far as position only. `79 79` extended frames are reassembled but their
   extra payload is not decoded.
3. **No device authentication** — the gateway trusts the IMEI in the login
   packet. Anyone who can reach the port can impersonate a device; keep it on
   a private segment and consider an IMEI allowlist.
4. **Batched writes trade a little durability for throughput** — fixes are
   ACKed as soon as they are queued and written as one `COPY` per batch, at
   least every `GATEWAY_FLUSH_INTERVAL` seconds. A gateway crash loses up to
   that many seconds of fixes the devices believe were delivered, and a
   failed write is logged, not retried. `GATEWAY_BATCH_SIZE=1` restores
   write-before-ACK. The queue is bounded, so a slow database pushes back on
   devices over TCP rather than growing memory. One flusher per process keeps
   each device's rows in arrival order; scale out with more gateway
   processes, not more flushers.
5. **TLS** — cheap trackers rarely support it, so the gateway listens on plain
   TCP. Positions and IMEIs travel unencrypted.
