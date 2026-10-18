#!/usr/bin/env bash
#
# Nightly upkeep for the AWS server, run by cron (installed by
# ensure-maintenance.sh, which every deploy runs):
#
#   1. back the database up   -- pg_dump, compressed, one file a day, the
#                                last BACKUP_KEEP_DAYS kept on this disk
#                                (and copied to S3 when BACKUP_S3_URI is set);
#   2. purge old positions    -- purge_old_locations() in sql/schema.sql,
#                                in batches, older than LOCATION_RETENTION_DAYS.
#
# Backup first, so a purge never removes anything that is not in last
# night's dump as well. Settings, all optional, in
# /etc/gps-tracking-maintenance.env:
#
#   LOCATION_RETENTION_DAYS=180   # 0 = keep everything; minimum 90
#   BACKUP_KEEP_DAYS=7
#   BACKUP_DIR=/var/backups/gps-tracking
#   BACKUP_S3_URI=s3://bucket/gps-tracking   # needs the aws CLI + an instance role
#   MAINT_PG_DSN=postgresql://...            # default: local Postgres as the postgres user
#   PGDATABASE=trackit
set -euo pipefail

CONF=/etc/gps-tracking-maintenance.env
# shellcheck disable=SC1090
[[ -r "$CONF" ]] && source "$CONF"

RETENTION_DAYS="${LOCATION_RETENTION_DAYS:-180}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-7}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/gps-tracking}"
DB="${PGDATABASE:-trackit}"

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }

if [[ -n "${MAINT_PG_DSN:-}" ]]; then
  run_psql() { psql "$MAINT_PG_DSN" "$@"; }
  run_dump() { pg_dump "$MAINT_PG_DSN" "$@"; }
else
  run_psql() { runuser -u postgres -- psql -d "$DB" "$@"; }
  run_dump() { runuser -u postgres -- pg_dump -d "$DB" "$@"; }
fi

# 1. Backup ------------------------------------------------------------------
install -d -m 700 "$BACKUP_DIR"
file="$BACKUP_DIR/$DB-$(date -u +%F).dump"
tmp="$file.partial"
# Custom format: compressed, and restorable table by table with pg_restore.
if run_dump -Fc -Z 6 > "$tmp"; then
  chmod 600 "$tmp" && mv "$tmp" "$file"
  log "backup ok: $file ($(du -h "$file" | cut -f1))"
else
  rm -f "$tmp"
  log "backup FAILED -- not purging tonight"
  exit 1
fi
find "$BACKUP_DIR" -name "$DB-*.dump" -mtime +"$KEEP_DAYS" -delete

if [[ -n "${BACKUP_S3_URI:-}" ]]; then
  if command -v aws >/dev/null; then
    # Infrequent Access: a backup is written once and rarely read, at about
    # half the price of standard storage. Expire old ones with a bucket
    # lifecycle rule rather than from here.
    aws s3 cp --only-show-errors --storage-class STANDARD_IA "$file" "${BACKUP_S3_URI%/}/" \
      && log "copied to ${BACKUP_S3_URI%/}/" || log "S3 copy FAILED"
  else
    log "BACKUP_S3_URI is set but the aws CLI is not installed; skipped"
  fi
fi

# 2. Retention ---------------------------------------------------------------
if [[ "$RETENTION_DAYS" == "0" ]]; then
  log "retention off (LOCATION_RETENTION_DAYS=0)"
else
  total=0
  while :; do
    n=$(run_psql -tAq -v ON_ERROR_STOP=1 -c "SELECT purge_old_locations($RETENTION_DAYS)")
    total=$((total + n))
    (( n == 0 )) && break
    sleep 1   # let the gateway's writes through between batches
  done
  log "purged $total rows older than $RETENTION_DAYS days"
  # Hand the freed pages back for reuse and refresh the planner's numbers.
  (( total > 0 )) && run_psql -q -c "VACUUM (ANALYZE) device_locations" && log "vacuumed"
fi

# Disk check: the one alarm this box needs.
used=$(df --output=pcent / | tail -1 | tr -dc '0-9')
(( used >= 85 )) && log "WARNING: root disk ${used}% full"
log "done"
