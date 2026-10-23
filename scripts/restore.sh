#!/usr/bin/env bash
# Restore friday's SQLite database from an encrypted backup blob.
#
# Usage:
#   scripts/restore.sh gs://my-friday-backups/friday-20261023T030017Z.db.enc
#   scripts/restore.sh /path/to/friday-backup.db.enc
#
# The blob is decrypted with the master key and its SQLite integrity is checked BEFORE it replaces
# the live database; a wrong key or a truncated download aborts without touching the current file.
# Stop friday before restoring so nothing writes during the swap.
set -euo pipefail

src="${1:?usage: restore.sh <gs://|s3://|path to .db.enc>}"
blob="$(mktemp)"
trap 'rm -f "$blob"' EXIT

case "$src" in
  gs://*) gcloud storage cp "$src" "$blob" ;;
  s3://*) aws s3 cp "$src" "$blob" ;;
  *)      cp "$src" "$blob" ;;
esac

python -m friday.backup restore "$blob"
echo "restore complete; start friday again"
