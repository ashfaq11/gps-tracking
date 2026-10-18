#!/usr/bin/env bash
# Install (or update) the nightly maintenance cron job: database backup,
# then purging positions past the retention period -- see maintenance.sh.
#
# Run by .github/workflows/deploy.yml from the repo checkout on every
# deploy; rewrites the cron file only when its contents would change.
set -euo pipefail

SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/maintenance.sh"
CRON_FILE=/etc/cron.d/gps-tracking-maintenance
LOG=/var/log/gps-maintenance.log

# 21:30 UTC is 03:00 IST: the quietest hour for trackers and people alike.
want="# Managed by deploy/aws/ensure-maintenance.sh -- edits are overwritten.
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
30 21 * * * root $SCRIPT >> $LOG 2>&1
# Keep the log itself small.
0 22 * * 0 root tail -n 2000 $LOG > $LOG.tmp && mv $LOG.tmp $LOG
"

chmod +x "$SCRIPT"
if [[ "$(sudo cat "$CRON_FILE" 2>/dev/null)" != "$want" ]]; then
  printf '%s' "$want" | sudo tee "$CRON_FILE" >/dev/null
  sudo chmod 644 "$CRON_FILE"
  echo "Maintenance: nightly backup + retention installed ($CRON_FILE)"
else
  echo "Maintenance: cron job already up to date"
fi
