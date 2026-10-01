#!/usr/bin/env bash
# Nightly backup of coach.db: consistent copy (SQLite online backup API, safe while
# the bot is writing), compressed, kept for 30 days.
set -euo pipefail
cd "$(dirname "$0")/.."

DEST="${BACKUP_DIR:-/home/coach/backups}"
KEEP_DAYS="${KEEP_DAYS:-30}"
mkdir -p "$DEST"

FILE="$DEST/coach-$(date +%F).db"
.venv/bin/python db.py backup "$FILE"
# Prove the copy opens and isn't corrupt before trusting it
.venv/bin/python -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); r=c.execute('PRAGMA integrity_check').fetchone()[0]; sys.exit(0 if r=='ok' else 'integrity check failed: '+r)" "$FILE"
gzip -f "$FILE"
find "$DEST" -name 'coach-*.db.gz' -mtime +"$KEEP_DAYS" -delete
echo "Backed up to $FILE.gz"
