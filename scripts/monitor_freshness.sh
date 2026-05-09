#!/usr/bin/env bash
# Single-tick monitoring snapshot. Called every minute by ScheduleWakeup.
# Output is one paragraph appended to MONITORING.md by the caller.
set -euo pipefail

ssh root@217.216.108.111 'bash -s' << 'EOF'
NOW=$(date -u +"%H:%M:%S")
echo "==tick== ${NOW} UTC"

# 1) Orderbook health from /api/screener/exchange-health (inside container —
#    Cloudflare blocks unauth API calls from the host shell).
docker exec wallet-monitor-app-1 python3 << 'PY'
import json, urllib.request, sys
try:
    r = urllib.request.urlopen("http://localhost:8000/api/screener/exchange-health", timeout=5)
    d = json.loads(r.read())["exchanges"]
except Exception as e:
    print(f"  ob: ERR {e}"); sys.exit(0)
stale_count = 0
zero_count = 0
problems = []
for ex, v in d.items():
    tot = v.get("orderbook_total") or 0
    st = v.get("orderbook_stale") or 0
    deg = v.get("orderbook_degraded") or 0
    ma = v.get("orderbook_max_age_s") or 0
    age = v.get("age_s") or 0
    if tot == 0:
        zero_count += 1
        problems.append(f"{ex}=0books")
    if st > 0 or deg > 0:
        stale_count += st + deg
        problems.append(f"{ex} stale={st}/deg={deg}/max={ma:.0f}s")
    if age > 60:
        problems.append(f"{ex} feed_age={age:.0f}s")
print(f"  ob: total_stale={stale_count} zero_books={zero_count}")
if problems:
    print(f"     problems: {' | '.join(problems[:8])}")
PY

# 2) User-stream states from container logs (last 90s)
echo "  streams (last 90s state changes):"
docker logs --since=90s wallet-monitor-app-1 2>&1 | grep "userstream" | grep -E "INIT →|→ LIVE|→ DEGRADED|→ DEAD" | tail -15 | sed 's/^/    /' || true
docker logs --since=90s wallet-monitor-app2-1 2>&1 | grep "userstream" | grep -E "INIT →|→ LIVE|→ DEGRADED|→ DEAD" | tail -15 | sed 's/^/    /' || true

# 3) Errors per venue in last minute (excludes the noisy patterns)
echo "  errors (last 60s, dedupe):"
docker logs --since=60s wallet-monitor-app-1 wallet-monitor-app2-1 wallet-monitor-go-fetcher-1 2>&1 \
    | grep -iE 'error|fail|warning' \
    | grep -iE 'trade|userstream|adapter|exchange|httpx|ws' \
    | grep -ivE 'slow client|subscribe send failed' \
    | head -10 | sed 's/^/    /' || true

# 4) Container uptime
echo "  containers:"
docker ps --format '    {{.Names}} {{.Status}}' | grep -E 'fetcher|app' || true

EOF
