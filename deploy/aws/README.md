# Deploying the API and gateway to AWS

One server runs both processes in Docker: the REST API on port 8000 (the
dashboard's Cloudflare worker forwards `/api/*` to it) and the GT06 gateway on
port 5023. Every merge to `main` can deploy itself through GitHub Actions.

| File | What it does |
|---|---|
| [`docker-compose.yml`](docker-compose.yml) | `api` and `gateway` containers, plus a `migrate` job that applies `sql/schema.sql` |
| [`deploy.sh`](deploy.sh) | pull `main` → build → apply schema → restart → health-check |
| [`../../.github/workflows/deploy.yml`](../../.github/workflows/deploy.yml) | on every push to `main`: run the tests, then `deploy.sh` over SSH |
| [`../../Dockerfile.api`](../../Dockerfile.api) | the API image (the gateway uses the root `Dockerfile`) |

## 1. One-time server setup

On the EC2 instance (Ubuntu shown; on Amazon Linux use `dnf install docker git`):

```bash
# Docker and the compose plugin
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git curl
sudo usermod -aG docker "$USER"        # log out and back in afterwards

# The checkout the deploy script updates
sudo mkdir -p /opt/gps-tracking && sudo chown "$USER" /opt/gps-tracking
git clone https://github.com/ashfaq11/gps-tracking.git /opt/gps-tracking
```

### The env file

Secrets live in `/etc/gps-tracking.env`, never in git. Both containers read it.

```bash
sudo install -m 600 -o "$USER" /dev/null /etc/gps-tracking.env
nano /etc/gps-tracking.env
```

```ini
# Required
PG_DSN=postgresql://USER:PASSWORD@HOST:5432/trackit
INGEST_API_KEY=<a long random string>

# First start only: creates the admin account if none exists
BOOTSTRAP_ADMIN_USERNAME=admin
BOOTSTRAP_ADMIN_PASSWORD=<a strong password>

# Optional: Web Push (python -m api.push genkey), CORS for a direct-origin dashboard.
# The GitHub deploy (systemd setup) adds the VAPID pair itself on first run
# -- see deploy/aws/ensure-vapid.sh -- and never replaces it afterwards.
# VAPID_PUBLIC_KEY=...
# VAPID_PRIVATE_KEY=...
# VAPID_SUBJECT=mailto:you@example.com
# API_CORS_ORIGINS=https://your-dashboard.example
```

`PG_DSN` depends on where Postgres runs:

- **Supabase / RDS / other hosted:** the provider's connection string as is.
  For Supabase use the 5432 session pooler (see the main README).
- **Postgres on this same server:** use `host.docker.internal` as the host,
  not `localhost` (inside a container, localhost is the container). Postgres
  must also accept the Docker bridge: `listen_addresses = '*'` in
  `postgresql.conf` and a `pg_hba.conf` line such as
  `host all all 172.16.0.0/12 scram-sha-256`, then restart Postgres.

### Ports

In the instance's security group, allow inbound **8000** (the Cloudflare
worker's requests) and **5023** (trackers). Cloudflare Workers do not have
fixed outbound IPs, so 8000 has to be open to `0.0.0.0/0`.

### First deploy

Stop whatever serves ports 8000 and 5023 today (a `python -m api` in tmux, an
older systemd unit...). Otherwise the containers cannot bind them:

```bash
sudo ss -ltnp '( sport = :8000 or sport = :5023 )'   # shows what to stop
cd /opt/gps-tracking && ./deploy/aws/deploy.sh
```

It prints each step and ends with `API healthy` and `Gateway accepting
connections`. Then check from outside:

```bash
curl -i http://<server>:8000/api/v1/stats/report     # 401 = the new API is live
```

## 2. Automatic deploys from GitHub

Create a key used for nothing but deploying:

```bash
# On your own machine
ssh-keygen -t ed25519 -C gps-tracking-deploy -f deploy_key -N ''
ssh-copy-id -i deploy_key.pub ubuntu@<server>        # or append it to ~/.ssh/authorized_keys
ssh-keyscan -H <server>                               # copy this output too
```

In GitHub, open **Settings → Secrets and variables → Actions → New repository
secret** and add:

| Secret | Value |
|---|---|
| `DEPLOY_HOST` | the server's address, e.g. `13.205.155.148` |
| `DEPLOY_USER` | the SSH user from step 1 (in the `docker` group) |
| `DEPLOY_SSH_KEY` | the contents of `deploy_key` (the private key) |
| `DEPLOY_KNOWN_HOSTS` | the `ssh-keyscan` output, so the runner verifies the server instead of trusting whatever answers |
| `DEPLOY_PATH` | optional, defaults to `/opt/gps-tracking` |

From then on every push to `main` runs the test suite, then SSHes in and runs
`deploy.sh`. A failing test blocks the deploy. To deploy without a new commit,
use **Actions → Deploy → Run workflow**. Until the secrets exist, the deploy
job skips with a notice rather than failing.

## Nightly maintenance: backups and data retention

Every deploy installs a cron job (`ensure-maintenance.sh`) that runs
[`maintenance.sh`](maintenance.sh) at 03:00 IST. It dumps the database to
`/var/backups/gps-tracking` (7 days kept; optional S3 copy), then deletes
positions older than 180 days, keeping each vehicle's last position. Tune it
in `/etc/gps-tracking-maintenance.env`; see the script's header. Output goes
to `/var/log/gps-maintenance.log`.

Restore a backup:

```bash
sudo -u postgres pg_restore -d trackit --clean --if-exists /var/backups/gps-tracking/trackit-YYYY-MM-DD.dump
```

Cost notes and the one-time console checklist: [COSTS.md](COSTS.md).

## Day to day

```bash
docker compose -f deploy/aws/docker-compose.yml --env-file /etc/gps-tracking.env ps
docker compose -f deploy/aws/docker-compose.yml --env-file /etc/gps-tracking.env logs -f api
```

**Log files** are outside the project, in `~/gps_gateway/logs` (the folder
next to the checkout; `GPS_LOG_DIR` in deploy.sh's environment moves it),
which deploy.sh creates and binds into both containers. One file per week,
Monday to Sunday (IST), named by those dates (ddmmyyyy), with the API's and
the gateway's **warnings and errors** -- the last 4 weeks are kept:

```bash
ls ~/gps_gateway/logs                      # 19102026_25102026.log, log-level
tail -f ~/gps_gateway/logs/19102026_25102026.log
```

or over HTTP as an admin: `GET /api/v1/logs`, then
`GET /api/v1/logs/19102026_25102026.log?tail=500&level=ERROR`.

The level is **ERROR**. To let warnings in too, without a restart:

```bash
curl -X PUT https://<api>/api/v1/logs/level -H "Authorization: Bearer <admin token>" \
     -H 'Content-Type: application/json' -d '{"level": "WARNING"}'
```

(`{"level": "ERROR"}` goes back.) It is kept across restarts and deploys.
To debug one tracker packet by packet, set `GATEWAY_MESSAGE_LOG=on` in the
env file and redeploy: the week's `*.packets.log` gets one readable line per
packet (`GATEWAY_MESSAGE_LOG_RAW=1` adds the raw hex).

**Roll back:** check out an earlier commit and deploy it as it is.

```bash
cd /opt/gps-tracking && git checkout <commit> && ./deploy/aws/deploy.sh --no-pull
git checkout main    # before the next normal deploy
```

The schema is additive (`ADD COLUMN IF NOT EXISTS` and similar), so older code
runs against a newer schema.

**Server-specific tweaks** (another port, memory limits): put them in
`deploy/aws/docker-compose.override.yml`. `deploy.sh` layers it on top, and
it is gitignored, so pulls never overwrite it.

**Expect a short gap at each deploy:** restarting the gateway drops every
tracker's socket, and trackers reconnect on their own within a minute or so.
