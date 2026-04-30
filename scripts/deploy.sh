#!/usr/bin/env bash
# Deploy by surface area — pull only what changed instead of always doing a
# full rolling-deploy. Run on the prod box from /root/wallet-monitor.
#
# Usage:
#   ./scripts/deploy.sh                  # auto-detect from `git diff`
#   ./scripts/deploy.sh frontend         # frontend only — no rebuild needed
#   ./scripts/deploy.sh backend          # rebuild app + app2 (data plane keeps running)
#   ./scripts/deploy.sh fetcher          # rebuild fetcher only
#   ./scripts/deploy.sh migrations       # alembic upgrade head + rolling app/app2
#   ./scripts/deploy.sh nginx            # reload nginx config (or recreate if mount inode flipped)
#   ./scripts/deploy.sh all              # legacy full rolling-deploy
#
# Auto mode looks at the diff between HEAD and origin/main pre-pull:
#   - frontend/ touched → frontend
#   - backend/ or app.py or fetcher/ touched → backend
#   - alembic/versions/*.py added → migrations
#   - nginx/nginx.conf touched → nginx
#   - everything else → full rolling-deploy

set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "✗ .env missing — refusing to deploy" >&2
  exit 1
fi

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

smoke() {
  echo
  echo "── Smoke test ──"
  curl -sk -o /dev/null -w "  https://avalant.xyz/api/health → %{http_code} (%{time_total}s)\n" https://avalant.xyz/api/health
  curl -sk -o /dev/null -w "  https://avalant.xyz/api/plans  → %{http_code} (%{time_total}s)\n" https://avalant.xyz/api/plans
  echo "✓ Done"
}

deploy_frontend() {
  step "Frontend — bind-mount picks up host changes, no rebuild"
  # Static files in ./frontend are mounted into containers as /app/frontend.
  # Just make sure the working tree has the latest code. Cache-Control
  # max-age=60 on JS/CSS means returning users see new code within ~1 min.
  step "Pulling latest code"
  git pull --ff-only
  echo "  → frontend ready, no container action needed"
  smoke
}

deploy_backend() {
  step "Pulling latest code"
  git pull --ff-only
  step "Rebuilding app (primary, runs migrations)"
  docker compose up -d --build app
  ensure_healthy app
  step "Rebuilding app2 (secondary, skips migrations)"
  docker compose up -d --build app2
  ensure_healthy app2
  smoke
}

deploy_fetcher() {
  step "Pulling latest code"
  git pull --ff-only
  step "Rebuilding fetcher (data-plane sidecar)"
  docker compose up -d --build fetcher
  echo "  → fetcher restarted; data plane re-warming (10–20 s)"
  smoke
}

deploy_go_fetcher() {
  step "Pulling latest code"
  git pull --ff-only
  step "Rebuilding go-fetcher (Go sidecar; runs alongside Python fetcher)"
  docker compose up -d --build go-fetcher
  echo "  → go-fetcher started; default shadow mode = /tmp/avalant_cache_go"
  echo "    cutover: set GO_FETCHER_CACHE_DIR=/tmp/avalant_cache in .env"
  smoke
}

deploy_migrations() {
  step "Pulling latest code"
  git pull --ff-only
  step "Running alembic upgrade head via app1 (read-only check first)"
  docker compose exec -T app alembic current
  docker compose exec -T app alembic upgrade head
  step "Rebuilding app + app2 to pick up new model code"
  docker compose up -d --build app
  ensure_healthy app
  docker compose up -d --build app2
  ensure_healthy app2
  smoke
}

deploy_nginx() {
  step "Pulling latest code"
  git pull --ff-only
  step "Reloading nginx"
  HOST_MD5=$(md5sum nginx/nginx.conf | awk '{print $1}')
  CTR_MD5=$(docker compose exec -T nginx md5sum /etc/nginx/nginx.conf 2>/dev/null | awk '{print $1}' || echo missing)
  if [ "$HOST_MD5" != "$CTR_MD5" ]; then
    echo "  → nginx.conf inode changed, recreating container"
    docker compose up -d --force-recreate nginx
  else
    docker compose exec -T nginx nginx -s reload
    echo "  → nginx reloaded in place"
  fi
  smoke
}

deploy_all() {
  ./scripts/rolling-deploy.sh
}

auto_detect() {
  step "Auto-detecting scope from incoming changes"
  git fetch origin main
  local diff
  diff=$(git diff --name-only HEAD origin/main 2>/dev/null || echo "")
  if [ -z "$diff" ]; then
    echo "  → nothing to pull, exiting"
    exit 0
  fi
  echo "  changed files:"
  echo "$diff" | sed 's/^/    /'
  local has_backend has_frontend has_fetcher has_migrations has_nginx has_compose
  has_backend=$(echo "$diff" | grep -E '^(backend/|app\.py$|requirements\.txt$|Dockerfile$)' || true)
  has_frontend=$(echo "$diff" | grep -E '^frontend/' || true)
  has_fetcher=$(echo "$diff" | grep -E '^fetcher/' || true)
  has_migrations=$(echo "$diff" | grep -E '^alembic/versions/' || true)
  has_nginx=$(echo "$diff" | grep -E '^nginx/' || true)
  has_compose=$(echo "$diff" | grep -E '^docker-compose\.yml$' || true)
  if [ -n "$has_compose" ]; then
    echo "  → docker-compose.yml changed: full deploy"
    deploy_all
    return
  fi
  if [ -n "$has_migrations" ]; then
    echo "  → migrations detected"
    deploy_migrations
    return
  fi
  local touched_extras=0
  if [ -n "$has_backend" ]; then deploy_backend; touched_extras=1; fi
  if [ -n "$has_fetcher" ]; then deploy_fetcher; touched_extras=1; fi
  if [ -n "$has_frontend" ] && [ "$touched_extras" -eq 0 ]; then deploy_frontend; touched_extras=1; fi
  if [ -n "$has_nginx" ]; then deploy_nginx; touched_extras=1; fi
  if [ "$touched_extras" -eq 0 ]; then
    echo "  → no recognised scope, falling back to full rolling-deploy"
    deploy_all
  fi
}

case "${1:-auto}" in
  frontend)    deploy_frontend ;;
  backend)     deploy_backend ;;
  fetcher)     deploy_fetcher ;;
  go-fetcher)  deploy_go_fetcher ;;
  migrations)  deploy_migrations ;;
  nginx)       deploy_nginx ;;
  all)         deploy_all ;;
  auto)        auto_detect ;;
  *)
    echo "Usage: $0 [auto|frontend|backend|fetcher|migrations|nginx|all]"
    exit 1
    ;;
esac
