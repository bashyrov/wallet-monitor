# avalant-fetcher (Go)

Drop-in replacement for the Python `fetcher/` hot path. Owns the orderbook
WS adapters, funding-rate WS adapters, REST backstops, file dumper, and
arbitrage compute. Web roles (auth/admin/payments/trade) stay on Python.

## Status

⚠️ **Work in progress on `rewrite/go-hot-path` branch — NOT in production.**
Will live alongside the Python fetcher during migration. See [PLAN.md](PLAN.md)
for phasing and the bug-resistance map (every Python regression we fixed has
a designated solution here).

## Architecture

See `PLAN.md` § "Архитектура". TL;DR:

- Same files (`/tmp/avalant_cache/books.<ex>.json`, `books.master.json`,
  `books.json`) — Python web reads them unchanged.
- Same Redis pub/sub channels (`book:subscribe`, `book:unsubscribe`).
- Same env-vars (`REDIS_URL`, `AVALANT_PREWARM_TOP_N`, `AVALANT_WORKER_EXCHANGES`,
  …).

## Building

```bash
cd go-fetcher
go build -o /tmp/avalant-fetcher ./cmd/fetcher
```

## Running locally

```bash
AVALANT_FETCHER_CACHE_DIR=/tmp/avalant_cache_go \
LOG_LEVEL=debug \
/tmp/avalant-fetcher
```

Tail the cache to verify writes:

```bash
watch -n1 'ls -la /tmp/avalant_cache_go/ ; jq -r "keys|length" /tmp/avalant_cache_go/books.json'
```

## Tests

```bash
go test ./... -race
go vet ./...
```

## Diff vs Python

`scripts/diff_books.sh` (added in Phase 1) compares per-symbol top-20
levels between the Python fetcher's output and Go's output every 30s.
Tolerance: 0.01% on price, 1% on size (orderbooks tick).

## Bug-resistance contract

Each known regression from the Python era has a single named place where
it must be solved. See `PLAN.md` § "Карта известных багов". Reviewing
adapter PRs: walk that table and confirm each item is addressed by name.

Examples:

- **Bug #1 (orjson→TEXT)**: Adapters never call `c.WriteMessage()` directly —
  always `ws.SendText(c, payload)`. The helper enforces `TextMessage`.
- **Bug #4 (Bitget V2 needs app-level "ping")**: Adapter's `Heartbeat()`
  returns `[]byte("ping")`; runner sends it every `HeartbeatInterval()`.
- **Bug #5 (BingX gzip Ping/Pong)**: Adapter's `PongFor()` catches inbound
  "Ping" frame and returns "Pong" — runner sends it.

## Layout

```
cmd/fetcher/        # entry point + supervisor
internal/
  config/           # env-var loading
  log/              # zerolog wrapper
  ws/               # adapter framework (interface, runner, policy backoff,
                    # send.go enforcing TEXT frames)
  cache/            # in-memory book + atomic file dumper
  canonical/        # per-exchange valid `limit` sets (bug #22)
  exchanges/        # per-venue adapter implementations (Phase 1+)
  funding/          # funding-rate WS framework (Phase 3)
  arb/              # cross-venue arb compute (Phase 3-4)
```
