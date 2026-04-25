#!/usr/bin/env bash
# Rolling deploy — replaces `app` then `app2` so users never see a 502.
#
# How it works:
#   1. nginx upstream `avalant_app` round-robins between app:8000 and
#      app2:8000.
#   2. We rebuild + restart `app` first. While it boots, nginx routes all
#      traffic to `app2` via the upstream's max_fails/fail_timeout
#      mechanism + proxy_next_upstream retry.
#   3. After `app` reports healthy, we rebuild `app2` the same way.
#   4. nginx -s reload picks up any new IPs from Docker DNS.
#
# Usage on prod:
#   cd /root/wallet-monitor
#   ./scripts/rolling-deploy.sh
#
# Optional: pass `--migrate-only` to run alembic in app1 then exit
# (useful when only the migration is changing).

set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "✗ .env missing — refusing to deploy" >&2
  exit 1
fi

if [ ! -f docker-compose.yml ]; then
  echo "✗ docker-compose.yml missing" >&2
  exit 1
fi

# Pull the freshest image dependencies (postgres, redis, nginx, certbot)
# and rebuild only the python services we own. The fetcher restart is
# unrelated to the web rolling-deploy — it's a separate one-shot since
# there's no replica to fail over to.

step() { echo; echo "── $* ──"; }

ensure_healthy() {
  local svc="$1"
  local timeout=60
  local elapsed=0
  while [ $elapsed -lt $timeout ]; do
    local state
    state=$(docker compose ps --format json "$svc" 2>/dev/null \
      | python3 -c "import json,sys
try:
    rows = [json.loads(l) for l in sys.stdin if l.strip()]
    print(rows[0].get('State', '') if rows else 'missing')
except Exception:
    print('parse-error')" 2>/dev/null || echo "missing")
    if [ "$state" = "running" ]; then
      # Quick TCP probe via a sidecar exec — uvicorn binds in <2s once
      # the container is up.
      if docker compose exec -T "$svc" python -c "import socket;s=socket.socket();s.settimeout(2);s.connect(('localhost',8000));s.close()" 2>/dev/null; then
        echo "✓ $svc healthy after ${elapsed}s"
        return 0
      fi
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done
  echo "✗ $svc not healthy after ${timeout}s" >&2
  docker compose logs --since=60s "$svc" 2>&1 | tail -30
  return 1
}

step "Pulling latest code"
git pull --ff-only

step "Rebuilding app (primary, runs migrations)"
docker compose up -d --build app
ensure_healthy app

step "Rebuilding app2 (secondary, skips migrations)"
docker compose up -d --build app2
ensure_healthy app2

# fetcher uses the same code as app/app2. There's no replica — restart
# in-place is the best we can do, but we still need to rebuild the image
# so background-job changes (TG bot, expiry notifier, alert service)
# actually ship.
step "Rebuilding fetcher (data-plane sidecar)"
docker compose up -d --build fetcher

step "Reloading nginx so it sees fresh upstream IPs"
# nginx -s reload picks up the new IPs but doesn't pick up nginx.conf
# changes when the bind mount points at an inode that was replaced by
# a sed-style atomic edit. Compare md5 of the host file vs the
# container view; if they differ, we have to recreate the container so
# the mount sees the new file. Reload is otherwise enough.
HOST_MD5=$(md5sum nginx/nginx.conf | awk '{print $1}')
CTR_MD5=$(docker compose exec -T nginx md5sum /etc/nginx/nginx.conf 2>/dev/null | awk '{print $1}' || echo missing)
if [ "$HOST_MD5" != "$CTR_MD5" ]; then
  echo "  nginx.conf inode changed — recreating nginx container"
  docker compose up -d --force-recreate nginx >/dev/null
else
  docker compose exec -T nginx nginx -s reload || {
    echo "  nginx reload failed — falling back to restart" >&2
    docker compose restart nginx
  }
fi

step "Smoke test"
sleep 2
curl -sk -o /dev/null -w "  https://avalant.xyz/api/health → %{http_code} (%{time_total}s)\n" https://avalant.xyz/api/health
curl -sk -o /dev/null -w "  https://avalant.xyz/api/plans  → %{http_code} (%{time_total}s)\n" https://avalant.xyz/api/plans

echo
echo "✓ Rolling deploy complete"
