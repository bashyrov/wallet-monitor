#!/usr/bin/env bash
# Avalant — PostgreSQL backup script
# Usage: ./backup.sh [retain_days]
# Example: ./backup.sh 7   (keep last 7 days, default 14)

set -euo pipefail

RETAIN=${1:-14}
BACKUP_DIR="$(cd "$(dirname "$0")" && pwd)/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
FILE="$BACKUP_DIR/avalant_$TIMESTAMP.sql.gz"

mkdir -p "$BACKUP_DIR"

# Load .env if present (for POSTGRES_PASSWORD)
if [ -f "$(dirname "$0")/.env" ]; then
  # shellcheck disable=SC1091
  set -a; source "$(dirname "$0")/.env"; set +a
fi

POSTGRES_PASSWORD=${POSTGRES_PASSWORD:-changeme}

echo "→ Backing up PostgreSQL..."
docker compose exec -T db \
  pg_dump -U wallet -d avalant \
  | gzip > "$FILE"

SIZE=$(du -sh "$FILE" | cut -f1)
BYTES=$(wc -c < "$FILE" | tr -d ' ')

# Sanity: a healthy dump for the current schema is at least ~10 KB. A
# 0-byte / very-tiny gzip means pg_dump errored mid-stream (common when
# DB credentials change without updating the .env). Fail loudly so cron
# notices it via the exit code.
if [ "$BYTES" -lt 10000 ]; then
  echo "✗ Backup looks suspiciously small ($BYTES bytes) — refusing to keep" >&2
  rm -f "$FILE"
  exit 2
fi

# Validate the gzip stream — catches truncated dumps before they replace
# yesterday's good backup.
if ! gunzip -t "$FILE" 2>/dev/null; then
  echo "✗ gunzip -t failed on $FILE — corrupt or truncated" >&2
  rm -f "$FILE"
  exit 2
fi

echo "✓ Saved: $FILE ($SIZE)"

# Remove backups older than RETAIN days
find "$BACKUP_DIR" -name "avalant_*.sql.gz" -mtime +"$RETAIN" -delete
REMAINING=$(find "$BACKUP_DIR" -name "avalant_*.sql.gz" | wc -l | tr -d ' ')
echo "  Kept $REMAINING backup(s), removed files older than ${RETAIN}d"

# Touch a heartbeat file so monitoring can `find -mmin -1500`
# (>25h since last touch ⇒ alert; a run was missed).
touch "$BACKUP_DIR/.last_success"
