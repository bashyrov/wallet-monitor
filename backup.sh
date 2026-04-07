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
echo "✓ Saved: $FILE ($SIZE)"

# Remove backups older than RETAIN days
find "$BACKUP_DIR" -name "avalant_*.sql.gz" -mtime +"$RETAIN" -delete
REMAINING=$(find "$BACKUP_DIR" -name "avalant_*.sql.gz" | wc -l | tr -d ' ')
echo "  Kept $REMAINING backup(s), removed files older than ${RETAIN}d"
