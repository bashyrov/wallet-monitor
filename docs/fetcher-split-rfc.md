# RFC: Split the fetcher into per-exchange worker processes

Status: **planning**
Owner: bashyrov
Last update: 2026-04-23

## Problem

The fetcher container today holds everything:

- 11 orderbook-WS adapters (async tasks on the main event loop)
- 11 funding-WS adapters (own sub-threads, but REST backstops and dump still
  touch the loop)
- arb recompute loop (thread-pool but the initiator is async)
- spot-short + dex-short refresh loops (own threads today ✓)
- prewarm hotlist + dump (partially moved to threads ✓)
- price cache loop + alert service + telegram bot

Under load, the single asyncio event loop on the fetcher gets stalled by any
slow path. Visible symptoms we've already tracked:

1. **1011 keepalive bursts** — when the loop delays a ping, the exchange
   drops us. We see this hit 3-5 venues in the same 1-6s window, roughly
   every 30-60s.
2. **Orderbook dump emptying** — once fixed by moving the dumper to a thread
   and widening `FILE_FRESH_MAX → STALE_SERVE_MAX`. Root cause was the dump
   task starving behind spot compute.
3. **Aster 3002 rate-limit** — fixed by `subscribe_delay=0.3s`. Was a
   burst-spike on reconnect.

`uvloop` (#92) addressed ~60% of scheduler pressure. Remaining risk: we still
run every WS + compute on one process, so a crash in any adapter's parser
bounces the whole fetcher.

## Goals

- **No single adapter can affect another.** A crash in KuCoin doesn't stall
  Binance. A slow compute doesn't stall funding WS pings.
- **Zero impact on freshness SLA** (≤5s per-symbol).
- **Graceful rollout**: new architecture runs alongside old until we cut over.

## Non-goals (phase 1)

- Horizontal sharding across hosts (still one VM).
- Rewriting adapters. The `FundingWSAdapter` / `WSAdapter` base classes stay.
- Replacing `books.json` / `funding.json` as the web contract. File-based
  inter-process sync keeps parity with web-role readers.

## Proposal

### Architecture

```
  ┌───────────────────────────────────────────────────────────┐
  │ fetcher-master (1 process)                                │
  │   · spawns + health-checks worker processes               │
  │   · merges per-worker *.json into canonical books.json    │
  │   · arb/spot/dex compute (unchanged: own threads)         │
  │   · alert service, tg bot, price loop                     │
  └──────────────┬────────────────────────┬───────────────────┘
                 │                        │
     ┌───────────▼─────┐      ┌───────────▼─────────┐
     │ ws-worker[N]    │ ...  │ ws-worker[N]        │
     │ · 1-3 exchanges │      │ · 1-3 exchanges     │
     │ · own uvloop    │      │ · own uvloop        │
     │ · writes        │      │ · writes            │
     │   books.<ex>.json      │   books.<ex>.json   │
     └─────────────────┘      └─────────────────────┘
```

### Shared-state contract

Workers never share Python memory — each owns its `_book_cache` and writes
atomically to `/tmp/avalant_cache/books.<exchange>.json` at the existing
`PREWARM_DUMP_S` cadence (500ms).

The **master** runs a light 200ms merger that reads the per-exchange files
and writes the canonical `books.json` the web role consumes. Cost: ~2-3ms
per tick; the dicts are already in-memory dicts with `(exchange, symbol)`
keys so merge is O(sum(files)).

Subscribe requests (`/ws/book` in the web role) keep writing to
`pending_subs.json`. The **master** drains and routes each (exchange, symbol)
to the owning worker via a per-worker input file
(`/tmp/avalant_cache/subs.<worker>.json`) or direct SIGUSR1 + reload.

## Milestones

| # | Milestone | ETA | Done? |
|---|-----------|-----|-------|
| M1 | POC: one exchange (Binance) in a child process, `books.binance.json`, master merges to `books.json` | 1 day | — |
| M2 | Master process manager: spawn, health, restart on crash | 1 day | — |
| M3 | Move all 11 orderbook-WS adapters to workers | 0.5 day | — |
| M4 | Move funding-WS adapters to workers (same pattern) | 0.5 day | — |
| M5 | Observability: `/api/health/fetcher` lists workers + last-tick each | 0.5 day | — |

Total ~3-4 days engineering + integration time.

## Rollout

1. M1-M2 ship as opt-in via `AVALANT_FETCHER_MODE=multiprocess` (default:
   legacy single-process). Both modes live side-by-side on the fetcher.
2. Flip the env var on prod. Watch `/api/health/feeds` freshness dashboard
   for 24h.
3. If stable: drop `AVALANT_FETCHER_MODE` flag, delete single-process path.

## Open questions

- **Worker assignment**: one exchange per worker (11 workers) vs group-by-host
  (binance+aster on one worker since both use Binance-compatible endpoints,
  etc.). Start with one-per-exchange for simplicity; optimise later.
- **Restart storms**: if a worker flaps (e.g. KuCoin JWT rotation bug),
  shouldn't hot-restart in a tight loop. Exponential backoff + max retries
  per window.
- **Graceful shutdown**: master sends SIGTERM, workers flush their dumps,
  then exit. Current code doesn't have flush-on-shutdown.

## Why not now?

- Event-loop pressure is already meaningfully eased by `uvloop`. Freshness
  SLA is currently held.
- Half-finished subprocess split is a worse failure mode than the current
  single-process setup (partial writes, stale merges, worker leaks).
- Work belongs in a focused block, not interleaved with UX tickets.

## Next step

When someone picks this up: start with M1 on a feature branch. Prototype in
`backend/services/orderbook_ws_worker.py` as a standalone `python -m`
entrypoint. Don't touch `fetcher/__main__.py` until the worker runs
end-to-end on its own.
