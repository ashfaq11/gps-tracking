#!/usr/bin/env bash
# Make sure the gps-api service has a VAPID key pair, so Web Push (vehicle
# started/stopped and geofence alerts with the tab closed) works.
#
# Run by .github/workflows/deploy.yml from the repo checkout, on every
# deploy. Does nothing once the keys exist: a new pair would orphan every
# browser already subscribed with the old public key, so keys are created
# once and never rotated here.
#
# The pair is generated on this server and written straight into the env
# file the service reads (mode 600); it is never printed, so it never
# reaches the deploy log.
set -euo pipefail

SERVICE=gps-api
DROPIN_DIR=/etc/systemd/system/$SERVICE.service.d
OWN_ENV=/etc/gps-api-push.env

# Already set inline in the unit (Environment=VAPID_PRIVATE_KEY=...)?
if systemctl show "$SERVICE" -p Environment --value | grep -q 'VAPID_PRIVATE_KEY=.'; then
  echo "Web Push: VAPID keys already configured"
  exit 0
fi

# The env files the unit reads; "-" marks an optional one.
mapfile -t FILES < <(systemctl show "$SERVICE" -p EnvironmentFiles --value \
  | grep -oE '[^ ]+ \(ignore_errors=(yes|no)\)' | cut -d' ' -f1 | sed 's/^-//')

for f in "${FILES[@]}"; do
  if sudo grep -qE '^\s*VAPID_PRIVATE_KEY=.' "$f" 2>/dev/null; then
    echo "Web Push: VAPID keys already configured"
    exit 0
  fi
done

# pywebpush (and py_vapid with it) is in requirements.txt; install it if
# this server predates that.
if ! python3 -c 'import pywebpush, py_vapid' 2>/dev/null; then
  pip3 install -r requirements.txt --break-system-packages -q
fi

# Write to the unit's own env file when it has one; otherwise give it one.
TARGET=${FILES[0]:-}
if [ -z "$TARGET" ]; then
  TARGET=$OWN_ENV
  sudo mkdir -p "$DROPIN_DIR"
  printf '[Service]\nEnvironmentFile=%s\n' "$TARGET" | sudo tee "$DROPIN_DIR/push.conf" >/dev/null
  sudo systemctl daemon-reload
fi
if [ ! -e "$TARGET" ]; then
  sudo install -m 600 /dev/null "$TARGET"
fi

KEYS=$(python3 -m api.push genkey)
{
  echo ""
  echo "# Web Push key pair, generated $(date -u +%Y-%m-%dT%H:%MZ) by deploy/aws/ensure-vapid.sh."
  echo "# Keep it: a new pair stops every existing browser subscription."
  echo "$KEYS"
} | sudo tee -a "$TARGET" >/dev/null
unset KEYS
echo "Web Push: generated a VAPID key pair into $TARGET"
