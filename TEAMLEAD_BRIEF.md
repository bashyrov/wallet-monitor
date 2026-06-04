# Avalant — Technical Brief for Tech Lead

## What is this

**Avalant** — crypto arbitrage screener and trade execution platform. The core product: real-time funding-rate spread scanner across 18+ CEX/perp-DEX venues. Users see which exchange pair has the best long/short basis, open positions from the UI, and track P&L.

Live at: `avalant.xyz`

---

## Stack

| Layer | Tech | Notes |
|-------|------|-------|
| **Data plane** | Go 1.25 (`go-fetcher`) | Owns all market data ingestion |
| **Web backend** | Python 3.13, FastAPI, uvicorn | 2 replicas behind nginx |
| **DB** | PostgreSQL 16 via PgBouncer | SQLite for local dev |
| **Cache bus** | Redis 7 | Pub/sub + per-key orderbook mirror |
| **Frontend** | Vanilla JS, no build step | Bind-mounted, hot-swap |
| **Infra** | Docker Compose, nginx, Let's Encrypt | Single VPS |

---

## Architecture: two-process split

```
                    go-fetcher (Go)                Python app
                   ┌────────────────┐             ┌──────────────┐
  18 funding WS ──▶│ funding.Store  │─500ms dump──▶│              │
  26 OB WS      ──▶│ cache.Store    │─100ms dump──▶│  FastAPI     │──▶ Browser
                   │                │◀─Redis sub──  │  (2 replicas)│
                   │  Arb compute   │─arb.json    ──▶│              │
                   │  200/500ms/30s │               └──────────────┘
                   │                │
                   │  WS Broadcaster│──▶ /ws/long-short, /ws/book
                   └────────────────┘     (nginx routes :8090 directly)
```

**Go fetcher** owns everything real-time:
- 26 WebSocket connections (orderbooks) + 18 funding-rate feeds
- Arb compute 5×/sec
- Writes files + Redis; Python only reads

**Python** handles:
- Auth, billing, admin, Telegram bots
- Trade execution (routes to Go via HTTP, fallback to Python adapters)
- Serves HTML + REST API

---

## Data flow: token → spread → screen

```
t=0       Bootstrap: top-1000 symbols by volume from funding.json
t=5s      Symbol Manager sends SetSymbols() to all 26 OB runners
t=5-45s   WS subscribe frames go out (chunked: 50-100 syms/frame)
t=<100ms  First orderbook snapshot arrives → cache.Store → Redis SETEX (TTL 10s)
t=100ms   Dumper writes books.{exchange}.json (atomic rename)
t=200ms   Arb compute reads funding.Store → writes arbitrage.json (top-1000)
t=100ms   WS broadcaster diffs arbitrage.json → pushes to all browser clients
```

**End-to-end latency venue → browser: ~200-400ms**

---

## Orderbook subscription mechanics

Every exchange has its own adapter (`internal/exchanges/<name>/futures.go`) implementing:

```go
type Adapter interface {
    URL(ctx) (string, error)          // WS endpoint (some need REST token first)
    BuildSubscribe([]string) [][]byte // subscribe frames (chunked)
    Parse([]byte) (*Snapshot, error)  // snapshot vs delta merge
    Heartbeat() []byte                // app-level keepalive
    HeartbeatInterval() time.Duration
    SubscribeDelay() time.Duration    // inter-frame delay (rate limits)
    MaxSymbols() int                  // per-connection cap
    DecompressGzip() bool             // HTX, BingX use gzip
    OnReconnect()                     // clear local state on reconnect
}
```

**Key numbers:**

| Exchange | Protocol | Chunk | Delay | Special |
|----------|----------|-------|-------|---------|
| Binance | URL-based (no subscribe cmd) | N/A | 250ms reconnect | 200 sym cap |
| OKX | `books` channel | 100/frame | 0ms | was using private channel — fixed |
| Bitget | `books15` channel | 50/frame | 200ms | 200/frame → error 30002 |
| KuCoin | REST token first | 1/frame | 350ms | 3msg/s rate limit |
| Hyperliquid | subscribe | all | 500ms | high latency |
| HTX, BingX | subscribe | all | 0ms | gzip compressed |

---

## Zombie connection detection

Classic problem: TCP alive (ping/pong working), data subscription silently dead. Was invisible for months on OKX/Bitget.

**Fix — two watchdog checks every 5s:**

```
Check 1: no frames at all for 90s → force reconnect
Check 2: subscribed but lastData not updated for 5 min → zombie → force reconnect
```

`lastData` tracks data frames only (not pong heartbeats). `subscribedAt` marks when subscribe was sent so we don't false-positive during the subscribe phase.

---

## Arb compute

Runs every **200ms**, reads in-memory `funding.Store`:

```
for each (symbol, long_ex, short_ex) cross:
    rawSpread = short_funding_rate - long_funding_rate
    netSpread = rawSpread - (feeOf(long_ex) + feeOf(short_ex)) × 2
    if |price_deviation| > 100%: skip  ← ticker collision filter
    hysteresis: show after 1s, keep for 90s after last seen
top-1000 by |netSpread| → arbitrage.json
```

Fees hardcoded per venue (e.g. Binance 0.04%, Hyperliquid 0.035%, MEXC 0.02%).

---

## Symbol lifecycle: subscribe → idle → unsubscribe

```
User opens /arb pair page
  → Python: PUBLISH book:subscribe "okx:BTC" to Redis
  → go-fetcher subscriber: mgr.Touch("okx", "BTC")  (TTL=120s)
  → Symbol Manager reconcile (every 5s): adds BTC to OKX's wanted set
  → Runner.SetSymbols() → delta subscribe (no reconnect)
  → Data flows

User closes tab / 120s inactivity
  → Touch expires → next reconcile removes BTC
  → Runner.SetSymbols(without BTC) → reconnect with new set
```

---

## Trade execution

17 of 18 venues ported to Go, Python is fallback:

```
POST /api/trade/open → trade_service.py
  → if venue in GO_TRADE_VENUES:
       POST http://go-fetcher:8090/internal/trade/open
       auth: X-Internal-Auth header
       on any error → fall through to Python adapter
  → else: Python adapter directly
```

Signing schemes per venue: HMAC-SHA256 hex (Binance/Bybit), HMAC-SHA256 base64 (OKX/KuCoin/Bitget), EIP-712 (Aster/Hyperliquid), SNIP-12 Pedersen (Paradex), Ed25519 (Backpack).

---

## What works well

- **Reliability**: zombie watchdog + dual-tier backoff means connections self-heal
- **Freshness**: 200ms arb compute, 100ms file dump, 50ms Redis throttle
- **Isolation**: Go crashes don't affect Python web; Python crashes don't stop data
- **Trade coverage**: 17/18 venues in Go, full test suite (100+ tests)
- **Schema migrations**: Alembic, automated on app startup, two-replica race-safe

---

## Known gaps / what to improve

| Issue | Impact | Effort |
|-------|--------|--------|
| Python reads files (not gRPC) | +100-500ms latency on screener | High |
| Binance blocked from Singapore IP | Binance feed dead without proxy | Low (1 SOCKS5 proxy) |
| KuCoin: 1 sym/frame + 350ms delay | 1000 sym = 350s subscribe time | Medium |
| `books.json` merge 1s stale | OB data can be 1s old in fallback | Low |
| No Prometheus in production | Can't alert on data freshness | Medium |
| arb.json 2-5MB JSON per 200ms tick | CPU burn on marshal | Medium |
| No resync on sequence gap (Kraken/HTX) | Stale book until reconnect | Low |

---

## Repo structure

```
wallet-monitor/
├── go-fetcher/              ← Go data plane (main product logic)
│   ├── cmd/fetcher/main.go  ← entry point, all goroutines
│   └── internal/
│       ├── exchanges/       ← 26 OB WS adapters
│       ├── funding/         ← 18 funding adapters
│       ├── arb/             ← futures/spot/dex compute
│       ├── ws/              ← runner, backoff, adapter interface
│       ├── cache/           ← store, dumper, pruner
│       ├── redisbus/        ← Redis write/subscribe
│       ├── symbols/         ← symbol manager (reconcile loop)
│       ├── wsbroadcast/     ← WS hub → browser
│       └── trade/           ← 17 trade adapters
├── backend/
│   ├── api/v1/              ← FastAPI routes
│   ├── services/            ← business logic
│   │   └── trade_adapters/  ← Python trade fallbacks
│   └── providers/           ← portfolio balance fetchers
├── frontend/                ← 27 HTML pages, 16 JS modules, no build step
├── alembic/versions/        ← 47 DB migrations
├── scripts/                 ← deploy.sh, rotate_encryption_key.py
├── CLAUDE.md                ← development guide (keep updated)
├── ARCHITECTURE.md          ← technical deep-dive with code examples
└── DEPLOY.md                ← deployment runbook
```

---

## Current prod baseline

- **Server**: 12-core EPYC, 48 GB RAM
- **Go fetcher**: ~9.5 cores (PREWARM=1000, 26 OB WS + 18 funding + 3 arb engines)
- **Python**: ~1.5 cores (near-zero traffic)
- **Headroom**: ~2,000 concurrent users before WS broadcaster becomes bottleneck
