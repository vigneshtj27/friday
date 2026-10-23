#!/usr/bin/env bash
# Nightly encrypted backup of friday's SQLite database to a private bucket.
#
# The database is snapshotted and encrypted locally (python -m friday.backup) with the same master
# key the vault uses; only the opaque blob leaves the machine. Point FRIDAY_BACKUP_BUCKET at a
# PRIVATE bucket (e.g. gs://my-friday-backups or s3://my-friday-backups); nothing here makes it
# public. Run it from cron, e.g.:  17 3 * * *  /app/scripts/backup.sh >> /var/log/friday-backup.log 2>&1
set -euo pipefail

: "${FRIDAY_BACKUP_BUCKET:?set FRIDAY_BACKUP_BUCKET to a private gs:// or s3:// path}"
ts="$(date -u +%Y%m%dT%H%M%SZ)"
blob="$(mktemp)"
trap 'rm -f "$blob"' EXIT

# Snapshot + encrypt locally. Needs the same FRIDAY_* env the app runs with (master key, data dir).
python -m friday.backup backup "$blob"

dest="${FRIDAY_BACKUP_BUCKET%/}/friday-${ts}.db.enc"
case "$FRIDAY_BACKUP_BUCKET" in
  gs://*) gcloud storage cp "$blob" "$dest" ;;
  s3://*) aws s3 cp "$blob" "$dest" ;;
  *)      cp "$blob" "$dest" ;;  # a local/NFS path, for a mounted private volume
esac
echo "backed up to $dest"

# Keep the most recent FRIDAY_BACKUP_KEEP (default 14); prune older ones where the CLI supports it.
keep="${FRIDAY_BACKUP_KEEP:-14}"
case "$FRIDAY_BACKUP_BUCKET" in
  gs://*) mapfile -t old < <(gcloud storage ls "${FRIDAY_BACKUP_BUCKET%/}/" | sort | head -n "-${keep}") || true
          [ "${#old[@]}" -gt 0 ] && gcloud storage rm "${old[@]}" || true ;;
esac
