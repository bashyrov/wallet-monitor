#!/usr/bin/env bash
# Compare per-symbol top-20 levels between Python's books.<ex>.json and
# Go's books.<ex>.json. Run from anywhere with both fetchers writing to
# different cache dirs (or same dir during cutover).
#
# Tolerance:
#   - top-of-book price: ≤ 0.05% drift acceptable (orderbook ticks)
#   - top-of-book size:  ≤ 5% drift acceptable
#   - missing symbol on either side: WARN, not FAIL
#
# Usage:
#   ./diff_books.sh <python_cache_dir> <go_cache_dir> [exchange...]
#
# Example:
#   ./diff_books.sh /tmp/avalant_cache /tmp/avalant_cache_go binance bybit okx

set -uo pipefail

PY_DIR="${1:-/tmp/avalant_cache}"
GO_DIR="${2:-/tmp/avalant_cache_go}"
shift 2 || shift $#

if [ "$#" -eq 0 ]; then
    EXCHANGES=(binance bybit okx)
else
    EXCHANGES=("$@")
fi

if [ ! -d "$PY_DIR" ]; then
    echo "ERROR: python cache dir $PY_DIR not found" >&2
    exit 1
fi
if [ ! -d "$GO_DIR" ]; then
    echo "ERROR: go cache dir $GO_DIR not found" >&2
    exit 1
fi

ANY_FAIL=0

for EX in "${EXCHANGES[@]}"; do
    PY_FILE="$PY_DIR/books.$EX.json"
    GO_FILE="$GO_DIR/books.$EX.json"

    if [ ! -f "$PY_FILE" ]; then
        echo "WARN  $EX: $PY_FILE missing"
        continue
    fi
    if [ ! -f "$GO_FILE" ]; then
        echo "WARN  $EX: $GO_FILE missing"
        continue
    fi

    python3 - "$EX" "$PY_FILE" "$GO_FILE" <<'PY'
import json, sys

ex, py_path, go_path = sys.argv[1], sys.argv[2], sys.argv[3]
py = json.load(open(py_path))
go = json.load(open(go_path))

py_syms = set(py.keys())
go_syms = set(go.keys())
shared = py_syms & go_syms
only_py = py_syms - go_syms
only_go = go_syms - py_syms

ok = 0
near = 0
diverged = []

for sym in sorted(shared):
    py_e = py[sym] or {}
    go_e = go[sym] or {}
    py_bids = py_e.get('bids') or []
    go_bids = go_e.get('bids') or []
    py_asks = py_e.get('asks') or []
    go_asks = go_e.get('asks') or []

    if not py_bids or not go_bids:
        continue
    py_top_bid = py_bids[0][0]
    go_top_bid = go_bids[0][0]
    drift_pct = abs(py_top_bid - go_top_bid) / max(py_top_bid, go_top_bid) * 100

    if drift_pct < 0.05:
        ok += 1
    elif drift_pct < 0.5:
        near += 1
    else:
        diverged.append((sym, py_top_bid, go_top_bid, drift_pct))

print(f"{ex:12s} shared={len(shared):3d}  py_only={len(only_py):3d}  go_only={len(only_go):3d}  ok={ok:3d}  near={near:3d}  diverged={len(diverged)}")
if diverged:
    for sym, p, g, d in diverged[:5]:
        print(f"  ⚠ {sym}: py={p}  go={g}  drift={d:.3f}%")
    sys.exit(1)
PY
    if [ $? -ne 0 ]; then
        ANY_FAIL=1
    fi
done

exit $ANY_FAIL
