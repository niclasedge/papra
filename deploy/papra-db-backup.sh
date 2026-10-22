#!/bin/sh
# Consistent snapshot of Papra's SQLite DB, once per hour, one file per day.
# `.backup` uses SQLite's online-backup API and sees the WAL; a plain copy of the
# .db can pass integrity_check and still miss rows. The check runs against the
# COPY, never the source. Dropbox keeps the version history of each daily file.
set -eu

apk add --no-cache sqlite >/dev/null

DB=/data/db/db.sqlite
OUT=/backup
RETENTION_DAYS=30

while :; do
  if [ -f "$DB" ]; then
    tmp="$OUT/.papra-snapshot.tmp"
    rm -f "$tmp"
    if sqlite3 "file:$DB?mode=ro" ".backup '$tmp'" \
      && [ "$(sqlite3 "$tmp" 'PRAGMA integrity_check')" = "ok" ]; then
      mv "$tmp" "$OUT/papra-$(date +%F).sqlite"
      echo "$(date -Iseconds) snapshot ok: papra-$(date +%F).sqlite"
    else
      rm -f "$tmp"
      echo "$(date -Iseconds) snapshot FAILED" >&2
    fi
    find "$OUT" -name 'papra-*.sqlite' -mtime +"$RETENTION_DAYS" -delete
  else
    echo "$(date -Iseconds) no database yet at $DB" >&2
  fi
  sleep 3600
done
