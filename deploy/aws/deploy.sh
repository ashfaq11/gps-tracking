#!/usr/bin/env bash
#
# Deploy main to this server: pull, build, apply the schema, restart, verify.
#
#   ./deploy/aws/deploy.sh             # what the GitHub Actions workflow runs
#   ./deploy/aws/deploy.sh --no-pull   # deploy the checkout as it is
#
# Safe to re-run. It stops on the first failure. Images are built before
# anything is stopped, so a build error leaves the running version alone, and
# the schema is applied before the new containers start, because the gateway
# refuses to start without record_ignition() and the API's queries expect the
# current columns.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${GPS_ENV_FILE:-/etc/gps-tracking.env}"
BRANCH="${DEPLOY_BRANCH:-main}"
API_URL="http://127.0.0.1:${API_PUBLISH_PORT:-8000}/api/v1/health"
GATEWAY_PORT="${GATEWAY_PUBLISH_PORT:-5023}"
PULL=1
[[ "${1:-}" == "--no-pull" ]] && PULL=0

step() { printf '\n==> %s\n' "$*"; }
die() { printf 'deploy: %s\n' "$*" >&2; exit 1; }

[[ -r "$ENV_FILE" ]] || die "cannot read $ENV_FILE -- create it first (see deploy/aws/README.md)"
grep -q '^PG_DSN=' "$ENV_FILE" || die "$ENV_FILE has no PG_DSN"
command -v docker >/dev/null || die "docker is not installed"

export GPS_ENV_FILE="$ENV_FILE"
# A docker-compose.override.yml next to the compose file, if present, is
# layered on top: server-specific tweaks (ports, limits) without editing a
# tracked file that the next pull would overwrite. It is gitignored.
COMPOSE_FILES=(-f "$REPO_DIR/deploy/aws/docker-compose.yml")
[[ -f "$REPO_DIR/deploy/aws/docker-compose.override.yml" ]] &&
  COMPOSE_FILES+=(-f "$REPO_DIR/deploy/aws/docker-compose.override.yml")
compose() { docker compose "${COMPOSE_FILES[@]}" --env-file "$ENV_FILE" "$@"; }

cd "$REPO_DIR"
if (( PULL )); then
  step "Updating to origin/$BRANCH"
  # Never deploy on top of local edits: they would silently ride along.
  [[ -z "$(git status --porcelain --untracked-files=no)" ]] || die "uncommitted changes in $REPO_DIR"
  git fetch --quiet origin "$BRANCH"
  git checkout --quiet "$BRANCH"
  git merge --quiet --ff-only "origin/$BRANCH"
fi
echo "Deploying $(git log -1 --format='%h %s')"

step "Building images"
compose build api gateway

step "Applying sql/schema.sql"
compose run --rm migrate

step "Starting containers"
compose up -d --remove-orphans api gateway

step "Checking the API"
for _ in $(seq 30); do
  if curl -fsS -o /dev/null "$API_URL"; then
    echo "API healthy: $API_URL"
    break
  fi
  sleep 2
done
curl -fsS -o /dev/null "$API_URL" || { compose logs --tail 50 api; die "API did not become healthy"; }

step "Checking the gateway"
for _ in $(seq 15); do
  if (exec 3<>"/dev/tcp/127.0.0.1/$GATEWAY_PORT") 2>/dev/null; then
    echo "Gateway accepting connections on :$GATEWAY_PORT"
    break
  fi
  sleep 2
done
(exec 3<>"/dev/tcp/127.0.0.1/$GATEWAY_PORT") 2>/dev/null || { compose logs --tail 50 gateway; die "gateway is not listening"; }

step "Done: $(git log -1 --format='%h')"
docker image prune -f >/dev/null || true
