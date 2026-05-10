# Avalant — Development Guide for Claude

## Recent work — TL;DR for new sessions

**PnL backfill from venue fills** (shipped 2026-05-09).

The reconcile worker was wired into web-role startup (was dormant since the Go-fetcher cutover — last `trade_positions` write was 2026-04-29). Going forward, externally-opened positions now flow into our DB.

Net-new: PnL tab "Sync" button does a 7-day fills + funding pull from each venue, materialises closed positions, and runs auto-pair detection. Spot/short basis pairs (closed spot LONG + closed futures SHORT same symbol within 12% notional / 5min) auto-pair as `pair_kind='spot_short'`. Manual Sync ⇆ / Unpair persists via existing `TradePairDecision`.

New tables: `trade_fills` (raw venue fills + funding events, idempotent via UNIQUE on `(wallet_id, exchange, market, kind, ext_trade_id)`), `fills_sync_cursor` (per-(wallet, exchange, market) high-watermark for delta pulls). `trade_positions` gained `leg_a_market` / `leg_b_market` (`futures` | `spot`) + `source` (`platform` | `reconcile` | `fills_backfill`).

API: `POST /api/trade/pnl/sync` (Redis-locked per user, 5-min TTL, runs in background), `GET /api/trade/pnl/sync` (status). Frontend auto-syncs on PnL-tab open if last sync > 30 min, manual button otherwise.

**Per-venue capability matrix** (fills backfill):

| Venue | Futures fills | Spot fills | Funding events | Realized PnL on fill | Notes |
|---|---|---|---|---|---|
| binance | ✓ /fapi/v1/userTrades | ✓ /api/v3/myTrades | ✓ /fapi/v1/income FUNDING_FEE | ✓ realizedPnl | Per-symbol sweep (endpoint requires symbol); symbols enumerated via /income |
| bybit | ✓ /v5/execution/list cat=linear | ✓ /v5/execution/list cat=spot | ✓ /v5/account/transaction-log type=SETTLEMENT | ✓ closedPnl | Cleanest API |
| okx | ✓ /api/v5/trade/fills-history SWAP | ✓ /api/v5/trade/fills-history SPOT | ✓ /api/v5/account/bills-archive type=8 | ✓ fillPnl | SWAP fillSz is in contracts — multiplied by ctVal from /instruments |
| gate | ✓ /api/v4/futures/usdt/my_trades | ✓ /api/v4/spot/my_trades | ✓ /api/v4/futures/usdt/account_book type=fund | · | Spot per-pair sweep from balances |
| kucoin | ✓ /api/v1/fills | · spot lives on different base URL | ✓ /api/v1/funding-history | · | Spot adapter not present today |
| bitget | ✓ /api/v2/mix/order/fills | · | ✓ /api/v2/mix/account/account-bill businessType=contract_settle_fee | ✓ profit | |
| bingx | ✓ /openApi/swap/v2/trade/allFillOrders (per-symbol) | · | ✓ /openApi/swap/v2/user/income FUNDING_FEE | partial | Per-symbol sweep via income |
| hyperliquid | ✓ /info userFillsByTime | · L1 spot product not exposed | ✓ /info userFunding | ✓ closedPnl | |
| aster | ✓ Binance-fork — same /fapi/v1/userTrades | · | ✓ /fapi/v1/income FUNDING_FEE | ✓ realizedPnl | Same shape as Binance |
| paradex | _todo_ | · | _todo_ | · | /v1/fills via Stark JWT — implement after first deploy verifies the rest |
| lighter | _todo_ | · | _todo_ | · | /api/v1/fills available; not yet implemented |
| ethereal | _todo_ | · | _todo_ | · | Socket.IO-only public stream; private fills endpoint TBD |
| backpack | _todo_ | _todo_ | _todo_ | · | /api/v1/history/fills — not yet implemented |
| kraken | _todo_ | · | _todo_ | · | /derivatives/api/v3/historicalexecutions — not yet implemented |
| whitebit | _todo_ | _todo_ | · | · | /api/v4/trade-account/executed-history — not yet implemented |
| htx | _todo_ | · | _todo_ | · | /linear-swap-api/v3/swap_financial_record — not yet implemented |
| mexc | **skip** | **skip** | **skip** | — | Per user decision; v3 futures endpoint deprecated |

`fills_backfill_service` skips MEXC explicitly. The remaining `_todo_` venues have `fetch_recent_fills` undefined — `hasattr` guard in the service skips them gracefully (no-op, no error). Adding them is mechanical: implement the per-venue fills + funding fetch per the table above.

**Reconstruction algorithm**: walk fills chronologically per (wallet × exchange × market × symbol), maintain net-qty + VWAP entry; on net→0 emit a `trade_positions` row (`source='fills_backfill'`, `opened_externally=closed_externally=True`). Idempotent — checks for existing row with matching wallet/exchange/market/side/symbol within ±2 min of the candidate's opened_at AND closed_at. Funding-kind fills are accumulated into the open position's `leg_a_funding_pnl_usd`.

---

**Orderbook WS fixes** (shipped 2026-05-07). HEAD: `f5a459d` on `main`.

Three fixes to go-fetcher orderbook WebSocket adapters that were silently dead:

- **OKX** (`go-fetcher/internal/exchanges/okx/futures.go`): channel was `books50-l2-tbt` (private, requires auth → error 60011). Switched to public `books` channel (same snapshot+delta wire format, ~400ms cadence). Also chunked subscribe to 100 symbols/frame.
- **Bitget** (`go-fetcher/internal/exchanges/bitget/futures.go`): 200-symbol frames triggered error 30002 "Unrecognized request". Reduced to 50 symbols/frame + 200ms `SubscribeDelay` between frames.
- **Zombie WS watchdog** (`go-fetcher/internal/ws/runner.go`): added `lastData` + `subscribedAt` tracking. After 5 min post-subscribe with no data frames, forces reconnect (was undetectable before because pong heartbeats kept `lastMsg` alive).

Root cause of zombie: `lastMsg` updated by pong responses so the 90s stale watchdog never fired even when subscriptions were silently rejected.

---

**Landing-style frontend redesign** (active, shipped to prod 2026-05-04). HEAD: `bb6c145` on `main`.

What changed:
- **Design tokens**: `frontend/design.css` is single source of truth for colours, radii, motion, fonts. Loaded by every page. Legacy aliases (`--green-dim`, `--teal-dim`, `--surface4`, `--font`, `--mono`, `--serif`, `--teal`) kept so 8 000+ lines of inherited page CSS keep cascading without rewrites.
- **Navbar**: `<app-navbar>` Web Component (`frontend/navbar.js` + `frontend/navbar.css`). Layout = flex with brand left, absolute-centered `.topbar-nav` (left:50% translateX(-50%)), right cluster `margin-left:auto`. Width 100%, padding 0 24px (no max-width — items used to drift right of the brand on wide screens because the side-1fr columns were unequal). Every item has an SVG icon. Active = filled `--green-soft` chip with `--green-edge` border. Mobile drawer ≤900px.
- **Homepage `/index.html`**: replaced with a copy of `/landing` minus the Pricing + FAQ sections (links go to `/pricing` page). Imports `auth.js`; on load toggles between `#nav-cta-guest` (Sign in / Get started) and `#nav-cta-user` (Open app + avatar). Removed an old pre-launch IIFE that stripped `href` from every product/auth link — that's why the CTAs used to look dead.
- **Buttons**: every `.btn*` pinned to `--r` (10px) corners via `!important` in design.css; no more pill. `:active` press feedback (translateY + brightness) added globally. Mockup "Trade" buttons on homepage wired to `/screener`, "Open paired position" to `/register`.
- **Profile (`/profile`)**: floating sticky sidebar (`top:88px`, `margin-left:8px`, soft shadow). Two tabs only — **Account** (hero + Balance History + Subscription + Telegram + Danger Zone, in that DOM order) and **Security** (2FA + API Keys wrapped in `<section id="sec-security">`). Pane switching via `.profile-content > [id^="sec-"].is-active`; URL hash drives initial state. Fraunces serif purged — `.section-title` had been inheriting it from design.css, fixed with explicit `font-family:'Inter'` in profile's local rule.
- **Auth pages** (login, register, password-reset, password-reset-confirm, verify-email, tg-done): rewritten with auth-card pattern, design.css buttons/inputs, Fraunces titles, glow-gradient background.
- **Other pages** (app, archive, watchlist, screener, arb, admin, admin-user): minimal-touch polish — radial glow `body::before`, design.css imported, page-local `:root` overrides stripped where present.

Quick deploy & rollback:
```bash
ssh root@217.216.108.111 "cd /root/wallet-monitor && ./scripts/deploy.sh frontend"     # frontend bind-mounted, no rebuild
ssh root@217.216.108.111 "cd /root/wallet-monitor && git checkout pre-redesign-v1 -- frontend/ && ./scripts/deploy.sh frontend"   # rollback tag
```

**Prod load (current baseline)**: 12-core EPYC, 48 GB RAM. go-fetcher fixed ~9.5 cores (24 ob-WS + 12 funding-WS + 3 arb engines). app+app2 ~1.5 cores under near-zero traffic. Headroom for ~2 000 concurrent users before WS broadcaster (single Go process) and Redis pub/sub become the ceiling. RAM is not a bottleneck at this scale (44 GB free).

**Things to know when continuing redesign work**:
- No frontend bundling — every page now loads `auth.js`, `navbar.js`, etc. directly. Edits propagate immediately, no rebuild step. The previous `dist/core.js`/`dist/aux.js` minified bundles + `build.mjs` were removed because the rebuild ergonomics weren't worth ~14 KB of minification gain (nginx already gzips).
- `landing.html` is intentionally NOT touched by the redesign — it's the visual reference and was self-contained before. The homepage `/index.html` is the propagated copy.
- Screener.html and arb.html are the heaviest pages (3 k / 6.6 k lines) and JS-coupled; the redesign treated them as polish-only (background gradient, design.css cascade), no markup edits.

## What is this project

**Avalant** — a web platform for crypto-arbitrage and portfolio management.

In one screen the trader sees funding-rate spreads (long/short between exchanges), spot/perp cash-and-carry, DEX/perp basis, and a unified portfolio view across CEX accounts + EVM/Solana/Tron addresses. Telegram bots handle login, alerts, and subscription notifications.

**Brand**: `avalant_` — Inter 800, 18px, blinking green `_` cursor. Accent `#1AFFAB` (neon green). Logo at `/avalant_favicon.svg`, full at `/avalant-logo.svg`.

**Supported venues**:
- **12 CEX** for screener + portfolio + trade: Binance, OKX, Bybit, Gate, MEXC, KuCoin, Bitget, Backpack, BingX, WhiteBIT, Kraken, HTX
- **6 Perp DEX**: Hyperliquid, Aster, Ethereal, Lighter, Paradex, Extended
- **8 Spot exchanges** for spot-short feeds: Binance, Bybit, OKX, Gate, KuCoin, MEXC, Bitget, BingX
- **15 chains**: Tron, Solana, Ethereum, BSC, Polygon, Arbitrum, Optimism, Base, Avalanche, Fantom, zkSync, Linea, Scroll, Mantle, Blast

**Trade-engine coverage** (Go-fetcher, after the perf/go-arb-and-trade port):

```
              screener  ob-WS  ob-REST  balance  trade-Go     funding-paid
binance       ✓         ✓      ✓        ✓        ✓ full       ✓
bybit         ✓         ✓      ✓        ✓        ✓ full       ✓
okx           ✓         ✓      ✓        ✓        ✓ full       ✓
gate          ✓         ✓      ✓        ✓        ✓ full       ✓
mexc          ✓         ✓      ✓        ✓        ✓ full       ✓
kucoin        ✓         ✓      ✓        ✓        ✓ full       ✓
bitget        ✓         ✓      ✓        ✓        ✓ full       ✓
bingx         ✓         ✓      ✓        ✓        ✓ full       ✓
htx           ✓         ✓      ✓        ✓        ✓ full       ·
aster         ✓         ✓      ✓        ✓        ✓ full       ✓
kraken        ✓         ✓      ✓        ✓        ✓ full       ✓
backpack      ✓         ✓      ✓        ✓        ✓ full       ·
whitebit      ✓         ✓      ✓        ✓        ✓ full       ·
hyperliquid   ✓         ·      ·        ✓        ✓ full       ✓
ethereal      ✓         ·      ·        ✓        ✓ full       ·
lighter       ✓         ·      ·        ✓        RO Go        ·
paradex       ✓         ✓      ✓        ✓        ✓ full       ·
extended      ✓         ·      ✓        ✓        ·            ·
```

Legend: `✓ full` open/close/leverage all in Go · `RO Go` reads in Go, writes return errZK · `·` not implemented.

**Blocked / partial:**
- **Lighter trading** — ZK signing requires the CGO-bundled `lighter-sdk` native lib (per-arch builds). Reads work in Go (`/api/v1/account` REST), writes return `KindUser` errors directing the dispatcher to the Python adapter. Keep `lighter` out of `GO_TRADE_VENUES`.
- **Extended trading** — `x10-python-trading` pins pydantic 2.5.3 / eth-account 0.11 / websockets 12 — would downgrade and break Hyperliquid + Ethereal adapters. Read-only.
- **Ethereal orderbook + user-stream** — public WS uses Socket.IO and SDK stream types (L2Book/Ticker/OrderFill) are rejected as "Invalid stream subscription type". REST API has no orderbook endpoint. Trade works.

---

## Stack

- **Python backend**: FastAPI + uvicorn (Python 3.13, 2 web replicas — `app` and `app2`), PostgreSQL 16 via PgBouncer (session mode, pool 60), Redis 7, httpx, websockets
- **Go data plane**: `go-fetcher` (Go 1.25), one container, owns funding-rate + orderbook WS, arb compute (futures/spot/dex), WS broadcast on `:8090`, internal trade engine on `/internal/trade/*`
- **Frontend**: vanilla JS + multi-page HTML, no build step, Inter / JetBrains Mono fonts, lightweight-charts via unpkg CDN
- **Infra**: Docker Compose 7 services, nginx upstream LB, Let's Encrypt via certbot
- **Bots**: two-bot mode — `TG_AUTH_BOT_TOKEN` for login + admin alerts + expiry reminders, `TG_BOT_TOKEN` for spread alerts. Either alone runs everything (single-bot fallback)

---

## Running

### Local
```bash
source venv/bin/activate
uvicorn app:app --port 8000   # NO --reload (user preference)
# DB: SQLite ./wallet_monitor.db, auto-created via Alembic
```

### Docker / prod
```bash
cp .env.sample .env             # fill SECRET_KEY, ENCRYPTION_KEY, POSTGRES_PASSWORD
docker compose up -d
```

Services on prod: `db`, `pgbouncer`, `redis`, `app`, `app2`, `go-fetcher`, `nginx`, `certbot`. App + app2 round-robin behind nginx; go-fetcher owns the data plane.

---

## Deployment workflow

See `DEPLOY.md` for the full picture. The prod box is at `root@avalant.xyz:/root/wallet-monitor`. There are 7 scopes:

| Command | What rebuilds | User impact |
|---|---|---|
| `./scripts/deploy.sh frontend` | nothing — `git pull`, files bind-mounted | new code on next request, ≤60s browser cache refresh |
| `./scripts/deploy.sh backend` | rolling rebuild app→app2 | zero downtime |
| `./scripts/deploy.sh fetcher` | go-fetcher only | 10–20s feed re-warm |
| `./scripts/deploy.sh migrations` | alembic + rolling app/app2 | brief; pair with maintenance for breaking schemas |
| `./scripts/deploy.sh nginx` | nginx reload (or recreate if mount inode flipped) | zero downtime |
| `./scripts/deploy.sh all` | everything via legacy `rolling-deploy.sh` | zero downtime |
| `./scripts/deploy.sh` (no arg) | auto-detects from `git diff origin/main` | scope-dependent |

`./frontend` is bind-mounted into all containers as `/app/frontend:ro` — HTML/JS/CSS edits hot-swap without rebuild. Cache-Control on JS/CSS is 60s; static images 86400s.

**Migration race-prevention**: `app` runs `alembic upgrade head` on startup (`AVALANT_RUN_MIGRATIONS=true`); `app2` skips it (`=false`). Alembic's row-lock on `alembic_version` serialises across the two containers.

**Rollback**: `git checkout <sha>` then `./scripts/deploy.sh all`.

**Things that don't need a deploy** (runtime via /admin):
- maintenance toggles + ETAs (3 scopes), banner, plans, promos, popups, billing periods
- hidden symbols, disabled exchanges, disabled chains, disabled perpdexes, disabled wallet exchanges
- trade enable/disable per venue (`trade_disabled_exchanges`)
- expiry-reminder schedule, admin broadcast
- user block/unblock, plan grant, per-user referral pct override

---

## Architecture: hot path

### Two-process model

After the Go-fetcher cutover, the data plane lives in Go and the web role (Python) only does the synchronous request handling.

**Python web (`app`, `app2`)**:
- Request handling, auth, billing, admin
- Reads file caches (`books.json`, `funding.json`, `arbitrage.json`, `spot_arbitrage.json`, `dex_arbitrage.json`) written by go-fetcher
- Telegram bot polling, expiry-notifier, alert daemon, plan-expiry daemon, reconcile_service
- Trade requests dispatch to go-fetcher via `/internal/trade/*` if venue is in `GO_TRADE_VENUES`; otherwise local Python adapter handles it. Falls back to Python on any Go error.

**Go-fetcher**:
- Funding-rate WS (12 venues) + 2s REST backstops
- Orderbook WS workers (24 adapters, filtered by `AVALANT_WORKER_EXCHANGES`)
- Arb compute (3 engines): futures every 500ms → `arbitrage.json`; spot every 2s → `spot_arbitrage.json`; dex every 30s → `dex_arbitrage.json`
- Cache + funding dumpers (100ms / 500ms cadences)
- WS broadcaster on `:8090` for `/api/screener/ws/{long-short,arb,funding,book}`
- `/internal/trade/*` HTTP endpoints (auth via `X-Internal-Auth` header == `AVALANT_INTERNAL_SECRET`)
- Symbols manager: prewarm union user-touches, reconciles every 5s
- Redis bus: receives `book:subscribe`/`book:unsubscribe` from Python web, mirrors orderbooks to `ob:<ex>:<sym>` keys (TTL 10s)

### Cache files (shared volume `/tmp/avalant_cache`)
- `books.<ex>.json` — per-exchange orderbook (Go writes)
- `books.json` — merged orderbook (Go writes)
- `funding.<ex>.json` — per-exchange funding (Go writes)
- `funding.json` — merged funding (Go writes)
- `arbitrage.json` — futures L/S opportunities (Go writes)
- `spot_arbitrage.json` — spot-short opportunities (Go writes)
- `dex_arbitrage.json` — DEX-short opportunities (Go writes)

Python web reads these directly. Lock files (`/tmp/avalant_*.lock`) prevent concurrent writes from any legacy fallback path.

### Funding feed (≤2.6s freshness across 12 venues, Go side)
Each adapter is a `Runner{ Adapter, Store }` with two concurrent loops:
1. **WS task** — primary sub-second updates from the venue's public stream
2. **REST backstop** — every 2s, sweeps all subscribed symbols and merges into `Store`. Required for every adapter (closes WS gaps).

Per-venue notes:
- **Binance**: `wss://fstream.binance.com/stream?streams=!markPrice@arr@1s/!ticker@arr` (combined stream). Known issue: times out from Singapore IP without outbound proxy.
- **MEXC**: REST-only (WS protobuf endpoint deferred).
- **Hyperliquid**: WS disabled (REST-only via `/info?type=fundingHistory`); symbol→index lookup cached 1h via `/info?type=meta`.
- **HTX**: REST-only (rate). Mark price comes from orderbook midprice cross-pollination.

### Spot-arb (Go)
`internal/arb/spot_compute.go` — REST tickers across 9 spot venues + funding join, writes `spot_arbitrage.json` every 2s. `|basis_pct| > 5%` rows dropped (ticker-collision filter; e.g. MEXC "META" ≠ KuCoin "META").

### DEX-arb (Go)
`internal/arb/dex_compute.go` — CoinGecko symbol→contract cache (1h TTL) + DexScreener pools, writes `dex_arbitrage.json` every 30s. Drops to 0 opps when DexScreener throttles; recovers next cycle.

### Futures arb compute (Go, 500ms cycle)
`internal/arb/compute.go` — reads funding store, builds cross-venue ranked top-1000 opportunities, writes `arbitrage.json` every 500ms. Downstream: WS broadcast diffs to clients.

### WS broadcast (Go-fetcher → browser)
- `/api/screener/ws/funding` — diff push, ~3-10KB per tick (note: clients have largely moved to 3s REST polling for funding; see frontend perf log)
- `/api/screener/ws/long-short` — delta-encoded futures arb (canonical)
- `/api/screener/ws/arb` — legacy alias for `/long-short`
- `/api/screener/ws/book` — orderbook diffs, server-side filtering by subscribed pairs

All four use first-frame JSON `{"auth": "<JWT>"}` after `accept()` (5s timeout → `close(4401)`). URL never carries the token.

### Orderbook in Go
- `internal/cache/Store` — concurrent-safe in-memory cache keyed by `"<ex>:<sym>"`, versioned per-venue
- `internal/cache/Dumper` — 100ms cadence writes per-venue `books.<ex>.json` + merged `books.json`
- `internal/redisbus/Writer` — mirrors each update to Redis (`ob:<ex>:<sym>`, TTL 10s) for fast `/ws/book` path
- `internal/symbols/Manager` — owns prewarm + user-touches union, calls `Runner.SetSymbols()` for deltas every 5s

---

## Trade engine

The trade engine has been ported from Python (`backend/services/trade_adapters/`) to Go (`go-fetcher/internal/trade/`). 16 of 17 venues are full-fat Go; lighter is read-only in Go (ZK signing not yet ported).

### Cutover model
1. Adapter self-registers via `init()` in Go.
2. `app`/`app2` POST trade requests to `http://go-fetcher:8090/internal/trade/{open,close,leverage,positions,balance}` if the venue is in `GO_TRADE_VENUES` (CSV env var on Python).
3. Auth via `X-Internal-Auth: <AVALANT_INTERNAL_SECRET>`.
4. Any Go error (5xx, network, `KindUser`, missing auth) → falls through to local Python adapter. **Python is never blocked.**
5. After 24h clean per venue, the Python adapter file can be removed from `ADAPTERS` and deleted.

### Signing schemes per venue (Go)

| Venue | Sign | Notes |
|---|---|---|
| binance | HMAC-SHA256 hex (`HMACHexSHA256`) | `X-MBX-APIKEY`, hedgeMode + exchangeInfo caches (5m / 10m) |
| bybit | HMAC-SHA256 hex (v5) | `ts\|\|key\|\|recv\|\|q/body` payload |
| mexc | HMAC-SHA256 hex | sorted query, coin qty encoding |
| bingx | HMAC-SHA256 hex | sorted query string |
| okx | HMAC-SHA256 base64 (`HMACBase64SHA256`) | passphrase required, contracts not coins (`÷ctVal`) |
| kucoin | HMAC-SHA256 base64 | passphrase required, UUID v4 client-order-id, coin↔contract conversion |
| bitget | HMAC-SHA256 base64 | passphrase, sign over `ts+method+path+body`, multiplier rounding |
| htx | HMAC-SHA256 base64 | multi-line canonical-string-to-sign, GET only |
| gate | HMAC-SHA512 hex (`HMACWith`) | preimage `method\|\|path\|\|sortedQuery\|\|sha512(body)\|\|ts`, coin↔contract, short side encoded as negative qty |
| whitebit | HMAC-SHA512 hex | preimage = base64(json_body) |
| kraken | HMAC-SHA512 raw → base64 | preimage = sha256(post + nonce + path); secret base64-decoded first; flavor unique to kraken-futures |
| **aster** | **EIP-712 typed data** | `signEIP712()` — domain `AsterSignTransaction`, chainId 1666, signature passed in `signature=0x…` query param; `X-AB-APIKEY` header. Both Python and Go use eth_account / go-ethereum |
| backpack | Ed25519 | seed-derived key over canonical sign string `instruction=…&sortedParams&timestamp=…&window=60000`; api_key is base64 public key |
| ethereal | personal_sign (eth_sign) | linked-signer key, payload `METHOD\|\|PATH\|\|TS_NS\|\|JSON(body)` |
| **hyperliquid** | **Phantom-agent EIP-712** | see below |
| **paradex** | **Stark SNIP-12** | see below |
| lighter | (writes not ported in Go) | reads work; writes return `errZK` |

### Hyperliquid — phantom-agent EIP-712 (real scheme)

Both Python and Go now sign actions per HL's official SDK. The earlier Python implementation used `personal_sign(sha256(action_json))` — that was wrong and would be rejected by `/exchange` on real orders.

```
packed       = msgpack(action) || nonce_be8 || vault_marker
vault_marker = 0x00                            if no vault
             | 0x01 || bytes20(vaultAddress)   otherwise
connectionId = keccak256(packed)
domain       = { name: "Exchange", version: "1",
                 chainId: 1337, verifyingContract: 0x0…0 }
type         = Agent(string source, bytes32 connectionId)
message      = { source: "a" (mainnet) | "b" (testnet), connectionId }
sig          = EIP-712 sign(domain, Agent, message) by agent key
wire         = { action, nonce, signature: {r,s,v}, vaultAddress: null }
```

**Cross-language parity is pinned** by:
- `TestPackAction_PythonParity` in [go-fetcher/internal/trade/hyperliquid/hyperliquid_test.go](go-fetcher/internal/trade/hyperliquid/hyperliquid_test.go) — Go's msgpack output equals Python's byte-for-byte
- `TestSignPhantomAgent_PythonParity` — `(r,s,v)` triple equals Python's for fixed key + nonce + action

If you change the action struct field declaration order in Go, msgpack output changes and signatures diverge. **Don't reorder.**

Python: [backend/services/trade_adapters/hyperliquid.py](backend/services/trade_adapters/hyperliquid.py) `_sign_action()`.
Go: [go-fetcher/internal/trade/hyperliquid/hyperliquid.go](go-fetcher/internal/trade/hyperliquid/hyperliquid.go) `signPhantomAgent()`.

### Paradex — Stark SNIP-12 in Go

Replaces the read-only Python provider's signing path. No more dependency on `paradex-py` (broken on Python 3.13). Pure Go via `github.com/NethermindEth/starknet.go` (Stark curve + SNIP-12 typed data + Pedersen).

```
chainIdHex   = hex(int.from_bytes(b"PRIVATE_SN_PARACLEAR_MAINNET", "big"))
auth message = SNIP-12 TypedData {
                 domain: { name:"Paradex", chainId:chainIdHex, version:"1" },
                 primaryType: "Request",
                 message: { method:"POST", path:"/v1/auth", body:"0",
                            timestamp, expiration }
               }
order message= SNIP-12 TypedData {
                 domain: same,
                 primaryType: "Order",
                 message: { timestamp, market, side: "1"|"2",
                            orderType: "MARKET"|"LIMIT",
                            size: felt(qty * 1e8),
                            price: felt(px * 1e8) | "0" for market }
               }
sig          = curve.SignFelts(messageHash, l2_priv_key)
hdr (auth)   = PARADEX-STARKNET-{ACCOUNT,SIGNATURE,SIGNATURE-EXPIRATION}
               + PARADEX-TIMESTAMP
hdr (data)   = Bearer JWT (cached 24h, refresh leeway 5min)
wire sig     = `["<r-decimal>","<s-decimal>"]`
```

**JWT cache**: `jwts map[lowercase_l2_addr]jwtEntry`, 24h TTL with 5-min refresh leeway. On 401 we drop the cache so the next call re-auths.

**Body field workaround**: Paradex sends `body: ""` in the auth message, but `starknet.go`'s `StrToHex("")` returns `"0x"` which fails to parse as felt. We pass `body: "0"` instead — both encode to felt 0, hash matches.

**Caveat**: tests cover internal consistency (Stark verify roundtrip, chain id pin, SNIP-12 message-hash, decimal output). There's NO live cross-vector against `paradex-py` (it doesn't load on 3.13 — the whole reason for the port). SNIP-12 is canonical so signatures *should* be accepted, but **the first real order on Paradex testnet is the truth check**. Keep `paradex` out of `GO_TRADE_VENUES` until that's been done.

Go: [go-fetcher/internal/trade/paradex/paradex.go](go-fetcher/internal/trade/paradex/paradex.go).

### Lighter — read-only in Go

Lighter signs orders with a per-account ZK key. Python uses CGO-bundled `lighter-sdk` shipped per-platform; there's no Go-native equivalent. Reads work in Go (unsigned `/api/v1/account`); trade actions return:

```go
var errZK = &trade.Error{
    Kind: trade.KindUser,
    Message: "lighter trade actions require ZK signing (lighter-sdk CGO) — not yet ported to Go; route this venue through the Python adapter (remove from GO_TRADE_VENUES).",
}
```

Credentials map: `APIKey` = numeric `account_index`, `APISecret` = hex private key, `Passphrase` = `api_key_index` (default `"255"`).

### Internal HTTP contract (go-fetcher:8090)

| Method | Path | Body |
|---|---|---|
| POST | `/internal/trade/open` | `{exchange, creds, request: OpenRequest}` → `Result` |
| POST | `/internal/trade/close` | `{exchange, creds, request: CloseRequest}` → `Result` |
| POST | `/internal/trade/leverage` | `{exchange, creds, request: LeverageRequest}` → 204 |
| POST | `/internal/trade/positions` | `{exchange, creds, symbol?}` → `[]Position` |
| POST | `/internal/trade/balance` | `{exchange, creds}` → `Balance` |
| GET | `/internal/trade/health` | — | `{supported: ["binance", ...]}` |

Auth header: `X-Internal-Auth: <AVALANT_INTERNAL_SECRET>` (read once at register time — secret rotation requires container restart).

### Trade signing helpers (Go)

`go-fetcher/internal/trade/signing.go` exports:
- `HMACHexSHA256(secret, payload)` — Binance/Bybit/MEXC/BingX
- `HMACBase64SHA256(secret, payload)` — OKX/KuCoin/Bitget/HTX
- `HMACBase64SHA512(secret, payload)` — Kraken (older path; current Kraken adapter uses `HMACWith` for the sha256-prehash + base64-encodes itself)
- `HMACWith(hashFunc, secret, payload)` — escape hatch (Gate uses `sha512.New` here for hex output; WhiteBIT same; Kraken with sha512 + post-encoding)
- `SortedFormQuery(map[string]string)` — deterministic urlencode

For exotic flavours, wrap in venue-local helpers — don't reach into the shared package. Aster (`signEIP712`), Backpack (`signEd25519`), Ethereal (`signPersonal`), Hyperliquid (`signPhantomAgent`), Paradex (`signTypedData`) all do this.

### Test counts (Go)

`binance:12 / bybit:7 / okx:9 / kucoin:6 / gate:7 / mexc:6 / bitget:5 / bingx:4 / htx:4 / whitebit:2 / kraken:3 / backpack:4 / aster:4 / ethereal:3 / hyperliquid:7 / lighter:3 / paradex:9` — total **100 tests** across 17 venues. Every venue has a `TestRegisteredViaInit` that confirms the adapter shows up via blank-import registration.

### Trade dispatcher (Python)

`backend/services/trade_service.py` — unified entry point. Enforces `plan.trade_delay_ms` on BOTH `place_open_order` AND `close_position` (Free plan: 500ms). Per-venue trade gate via `admin_settings.get_trade_disabled_exchanges()` blocks new positions on selected venues without disabling screener/funding/portfolio.

Order types: market only. Limit/stop/TP listed in `TODO.md`.

### Pair detection (spot-short)

`list_user_spot_short_pairs()` in `trade_service.py` cross-references open SHORT futures positions with non-stable spot holdings from `BalanceSnapshot.totals`. Auto-pairs when notional matches within ±12% (was 5%, bumped due to mirror-pair UX feedback) AND (if the spot snapshot is fresh) the spot was last refreshed within ±10 min of the short open. Manual paired/unpaired decisions persist in `TradePairDecision` with `leg_a_key` prefix `spot|`.

Endpoint: `GET /api/trade/spot-short-pairs`. Frontend `arb.html` uses `_accFetchSpotShortPairs()` + `_spotShortToPair()` to render spot positions in the per-pair panel.

### Funding-paid tracking

`funding_pnl_usd` populated on live positions for: binance, bybit, okx, aster, gate, kucoin, mexc, bitget, bingx, hyperliquid, kraken, lighter, htx. `reconcile_service` mirrors the field into `leg_a_funding_pnl_usd` so closed-pair P&L correctly nets out funding cost.

---

## Plan system

**Active paid tiers**: Free (5 wallets, 1 key/venue), Screener-only ($45/mo, 0 wallets, 3 keys/venue), Full ($55/mo, 30 wallets, 3 keys/venue), Enterprise (inactive — kept for future), Unlim (admin-only, -1 = unlimited).

**Billing periods** (4): Scout (1mo, 0% off), Operator (3mo, -10%), Season (6mo, -18%), Desk (12mo, -25%).

`-1` on `portfolio_limit` / `exchange_keys_per_venue` / `portfolio_limit_grace` = unlimited. Surfaced as `null` to the frontend.

**Source of truth**: `users.plan_id` (FK → plans). The legacy `users.plan` string column is a mirror. `plan_service.get_user_plan(db, user)` is the only correct read path.

**Plan upgrade invariant** — `user.plan_id` can only change via:
1. PATCH `/api/admin/users/{id}/plan` (admin-only, `Depends(get_admin_user)`)
2. `payment_service._activate_user` (signature-verified webhook only)
3. Manual SQL on the host

No client-controlled path grants a plan upgrade.

**Admin grant invariant** — `users.is_admin = TRUE` can only happen via:
1. Manual SQL on the host (`UPDATE users SET is_admin=TRUE WHERE …`)

The legacy `INITIAL_ADMIN_USERNAME` and `AVALANT_ALLOW_FIRST_USER_ADMIN` env-var paths were removed (2026-05-02). The TG-widget login also never grants admin. There is no API surface — registering, logging in, linking TG, no combination produces admin.

**Auto-archive on downgrade**: `wallet_quota.enforce_for_user(db, user)` is called from `set_plan` and from `/api/auth/me`. Surplus portfolio wallets archive oldest-first; `purpose='both'` rows downgrade to `'screener'`.

---

## Subscription mgmt

- **Auto-renew flag** (`users.auto_renew`, default True). Cancel sets it False — plan stays active until `plan_expires_at`, but expiry reminders stop firing.
- **Cancel/resume**: `POST /api/auth/me/subscription/cancel` and `/resume`. Profile page shows the right state (Active / Cancelled) + Renew + Cancel/Resume buttons.
- **Expiry-reminder daemon**: `backend/services/expiry_notifier_service.py` runs every 30 min on the web role. Scans users with `auto_renew=True` + `plan_expires_at` within `expiry_notice_days` (default 3, range 0–60) + `tg_chat_id` set. Sends via auth bot. Per-user throttle via `users.expiry_notice_last_sent_at` so daemon restarts don't double-fire. Schedule is admin-tunable from `/admin → Communications`.
- **Plan expiry daemon**: `backend/services/plan_expiry_service.py` runs every 10 min. Downgrades expired-plan users back to Free.
- **Renew flow**: button on /profile → `/pricing?renew=<plan-slug>`.

---

## Maintenance modes

Three independent scopes, each with optional ETA + countdown + IANA timezone (default `Europe/Warsaw`).

| Scope | Blocks | Stays open |
|---|---|---|
| `site` | every page | `/api/health`, `/api/maintenance/status`, admin API |
| `screener` | `/screener`, `/arb`, `/watchlist`, `/api/screener/*` | portfolio + admin + pricing |
| `portfolio` | `/app`, `/archive`, `/profile`, `/avashare`, wallet/alert/trade APIs | screener + pricing + checkout |

**Lockout pages auto-reload** when the scope flips back off — they poll `/api/maintenance/status` every 15s.

**Set ETA in one round-trip** (admin):
```bash
curl -X POST https://avalant.xyz/api/admin/maintenance \
  -H "Authorization: Bearer $JWT" -H 'Content-Type: application/json' \
  -d '{"scope":"screener","enabled":true,"duration_minutes":45}'
```

Display tz settable via `/admin → Maintenance`.

---

## Security

### Authentication
- JWT bearer (HS256), `jti` revocation in Redis (`backend/services/token_blacklist.py`)
- Bcrypt passwords (passlib 1.7.4 + bcrypt>=4,<5)
- `scope` claim on TOTP-challenge tokens — rejected by `get_current_user` so a leaked challenge can't masquerade as a session
- HttpOnly + Secure session cookie (override `AVALANT_COOKIE_SECURE=0` for localhost dev only)
- 30-day rolling cookie + JWT lifetime
- Per-account login lockout: `login_throttle.py` counts failed attempts on `users.failed_login_attempts`, auto-locks at threshold

### Admin
- **Promotion is SQL-only**: there is exactly one path — direct UPDATE on the host. `INITIAL_ADMIN_USERNAME` and `AVALANT_ALLOW_FIRST_USER_ADMIN` are no longer honoured. TG-widget login never grants admin.
- **TOTP 2FA** (admin-only): `pyotp` + Fernet-encrypted secret at rest. `users.totp_verified_at` is the armed flag. Login flow gates admin sessions on a second factor when `totp_verified_at` is set. Failed TOTP triggers `admin_alert_service.alert_admin_security` to admin TG.
- **Honeypot autoban** (`backend/services/honeypot_service.py`): a logged-in non-admin who hits `/api/admin/*`, `/admin`, or `/admin-user` is auto-blocked, audit-logged (`security.admin_probe_block`), and admins get a TG ping. Anonymous probes get plain 401 — too noisy to ban automatically.

### Rate limit
- **Backend bucket** (`backend/services/rate_limit.py`): Redis-backed sliding-window via `INCR + EXPIRE`. Falls back to in-memory on Redis blip with 10s backoff.
  - `payments_checkout` (5/min), `promo_validate` (10/min), `wallets_create` (30/h), `admin_write` (60/min)
- **Per-IP attempt counter** in `auth.py` on `/auth/*` (10/60s)
- **nginx zone limits** (in addition to backend):
  - `auth` zone — 5 r/m + burst 10 on `/api/auth/*`
  - `hot` zone — 20 r/m + burst 5 on `/api/portfolio/balance`
  - `api` zone — 60 r/s + burst 40 on `/api/*` (generic fallback)
  - `ws` zone — `limit_conn 10` per IP for WebSockets

### Encryption
- Fernet on `wallets.credentials` (PBKDF2-SHA256 from `ENCRYPTION_KEY`, 260k iterations, hardcoded salt `b"wallet-monitor-creds-v1"`)
- Same on `users.totp_secret_enc`
- Cached Fernet instance — derive once per process
- **Rotation**: `scripts/rotate_encryption_key.py` re-encrypts every wallet credential + every TOTP secret. Reads `AVALANT_OLD_ENCRYPTION_KEY`/`AVALANT_NEW_ENCRYPTION_KEY` from env; partial-rotation safe (each row tries OLD then NEW).

### Headers
- `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `X-XSS-Protection: 1; mode=block`
- `Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy: geolocation=(), camera=(), microphone=()`
- **CSP**: `default-src 'self'; script-src 'self' 'unsafe-inline' https://unpkg.com; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; ...; frame-ancestors 'none'; upgrade-insecure-requests`. Skipped on `/api/*`. `unpkg.com` in script-src is for `lightweight-charts` on `/arb`.
- HSTS 1-year via nginx
- OpenAPI hidden (`docs_url=None, redoc_url=None, openapi_url=None`)

### WebSocket auth
- First-frame `{"auth": "<JWT>"}` after `accept()`. URL no longer carries the token (used to leak into nginx access logs).
- 5s timeout → `close(4401)`. Applied to `/ws/funding`, `/ws/long-short`, `/ws/arb` (legacy alias), `/ws/book`.
- nginx routes `/api/screener/ws/(long-short|arb|funding|book)` to `go-fetcher:8090` directly. Other WS paths (legacy) hit `app:8000`.

### CryptoCloud webhook
- Strict signature validation (`payment_service.verify_webhook_signature`). If `CRYPTOCLOUD_WEBHOOK_SECRET` is unset, the route returns 503 + critical-level log (refuses, never fail-open).
- Bad signature → 401 + WARNING with source IP + payload key list.
- Idempotent: `SELECT … FOR UPDATE` on payments row + early-return if already paid.

### CSRF
Intentionally NOT added — every `/api/*` uses `Authorization: Bearer` + JSON body, never cookie form-post. The HttpOnly session cookie only gates HTML rendering in `serve_page`. CSRF tokens would add ceremony without closing real surface.

---

## Telegram bots

**Two-bot deployment**:
- `TG_AUTH_BOT_TOKEN` / `TG_AUTH_BOT_USERNAME` — login flow, admin alerts, expiry reminders, broadcasts
- `TG_BOT_TOKEN` / `TG_BOT_USERNAME` — arb-spread alerts to users (`alert_service`)

Either token alone runs everything (single-bot fallback). Auth-bot helpers `_auth_bot_token()` / `_auth_bot_username()` in `tg_auth_service.py` always prefer the auth bot when configured.

### Leader election (`backend/services/tg_bot_service.py`)
Both web replicas import `start_tg_bot()`, but only ONE process polls each bot at a time. Redis SETNX with TTL 30s + compare-and-set Lua renew every 10s (`tg_bot_lock:<sha256(token)[:16]>`). If the leader crashes, the next replica picks up within 30s.

Without `REDIS_URL`, falls back to single-replica polling — same race as before but no cross-replica coordination.

### Login flow
1. User opens `/login` → clicks "Sign in with Telegram"
2. Frontend issues `POST /api/auth/tg-bot-login` → gets `deep_link: t.me/<auth_bot>?start=auth-<token>`
3. User taps Start in the bot
4. Bot's `_handle_update` calls `consume_login_token` → writes JWT to `/tmp/avalant_cache/login_<hash>.json` AND replies with an inline-keyboard button "🔓 Open Avalant" → `https://avalant.xyz/tg-done?t=<token>`
5. User taps button → `tg-done.html` calls `GET /api/auth/tg-bot-login?token=X` → `Auth.setSession`, redirect to `/app`

The button is the load-bearing fix — mobile browsers freeze the originating /login tab when TG opens, killing the polling. Button-driven redirect bypasses that.

### tg_username refresh
TG usernames can change but `tg_id` + `tg_chat_id` are stable. Every `/start` resyncs `users.tg_username` to the current handle so the legacy username-fallback path keeps working.

### Admin broadcast
`POST /api/admin/broadcast {text, target='all'|'user', target_user_id?, parse_mode='HTML'}`. Sends via auth-bot, concurrency cap 20, audit-logged. Cap 4000 chars. Tab in `/admin → Communications → Broadcast`.

### Delisted-symbol filter (Binance specifically)
Binance keeps delisted symbols in `/api/v3/ticker/24hr` (`status=BREAK`) and `/fapi/v1/premiumIndex` (`status=SETTLING`) for days. We cross-check against `/exchangeInfo`'s `status=='TRADING'` set, cached 10 min. Applies to:
- `spot_arbitrage_service._fetch_binance_spot` (via `isSpotTradingAllowed`)
- `arbitrage_service._fetch_binance` (via `contractType=PERPETUAL`)
- Go's `internal/funding/binance` adapter — both WS push and REST backstop. Aster inherits via subclass.

NTRN is the canary — if it appears in the feed, the filter is broken.

---

## Promo codes

Each promo can grant a price discount AND/OR bonus subscription days, scoped per-user or per-target.

**Fields** (`promo_codes`):
- `discount_pct` (0–100) — % off the cart total
- `bonus_days` (0–3650) — days appended to `payment.activated_until`
- `max_uses` (null = ∞) — total usage cap
- `per_user_max_uses` (null = ∞, 1 = once per user) — caps PromoCodeUsage rows for the same user
- `target_user_id` (FK → users, null = anyone) — code only redeemable by this user
- `applies_to_plan_ids` (JSON list, null = all paid plans)
- `expires_at`, `is_active`

**Invariant**: at least one of `discount_pct > 0` or `bonus_days > 0` must hold. Pure trial extensions are valid (`EARLY7` = 0% + 7 days).

**Validation flow**: `validate_for_plan(db, code, plan_id, user_id=...)` is the canonical gate. Called from `/api/promo/validate` (frontend pre-check) AND from `payment_service.create_invoice` (server-side, before invoice creation). Both pass `user_id` so per-user + target enforcement work.

**Activation**: `_activate_user` reads the promo's `bonus_days` and adds to `activated_until` after the regular billing-period window. Logged at INFO.

**Default per-user cap**: All promos default `per_user_max_uses=1` (was bonus-only; broadened in commit `14295af` so a single user can't restack a discount code).

---

## Referrals (Avashare)

`/avashare` page + `referral_service.py` + `referral_payout_request` + `referral_earnings` tables.

- **Code**: each user gets a `referral_code` (random 6-char), `referred_by_id` (FK → users) populated on register if `?ref=` in URL.
- **Commission**: default % from `app_settings`, per-user override via `users.referral_pct_override`, partner-aware logic in `referral_service`.
- **Payout**: USDT-TRC20. `POST /referrals/me/payout` creates a `referral_payout_request{status: 'pending'}`. Admin reviews + flips to `'paid'` or `'rejected'` via `/admin → Communications → Referrals`. Payout floor is admin-configurable (`min_referral_payout_usd` setting).
- **Reversal**: `referral_earnings` has `reversed_at`, `reversal_reason`, `reversal_of_id` (self-FK). When a payment is refunded (admin-triggered), the earning is reversed atomically. See commit `eabb314`.
- **CHECK constraint** on `referral_payout_requests.status` — only `pending`/`paid`/`rejected` accepted.

---

## Popups

Admin-managed targeting popups, four audience modes:

| `target_type` | Audience |
|---|---|
| `everyone` | both authenticated and anonymous visitors |
| `authenticated` | every logged-in user (legacy `all` migrates here) |
| `anonymous` | logged-out visitors only |
| `user` | specific `target_user_id` |

`/api/popups/pending` is auth-optional — anonymous visitors get `everyone + anonymous`, logged-in get `everyone + authenticated + matching user`. Anonymous dismissals stored in `localStorage["avalant_popup_anon_dismissed"]` (no DB row possible without user_id).

Popup styling: backdrop with green radial glow, ID-pill in header, footer with "Maybe later" + CTA. `popup.js` is included on every auth-protected page + `/pricing`.

---

## Database

Production: PostgreSQL 16 via PgBouncer (session mode, pool 60). Local: SQLite. Migrations run automatically on `app` startup via `alembic upgrade head` (gated by `AVALANT_RUN_MIGRATIONS=true`; `app2` skips with `=false`).

### Tables (behavior-critical fields)
- **users** — id, username, email, hashed_password, is_admin, is_blocked, plan (legacy), plan_id (FK), plan_expires_at, request_count, last_active_at, created_at, email_verified_at, tg_username, tg_chat_id, tg_id, totp_secret_enc, totp_verified_at, **auto_renew**, **expiry_notice_last_sent_at**, **failed_login_attempts**, **referral_code**, **referred_by_id**, **referral_pct_override**, **referral_payout_address**
- **wallets** — id, name, wallet_type, type_value, credentials (JSON, Fernet), is_archived, can_trade (legacy), purpose ('portfolio' | 'screener' | 'both'), is_main, user_id
- **wallet_addresses** — named addresses for the address book
- **tags** — id, name, color, user_id (NULL = system tag)
- **wallet_tags** — M2M, CASCADE
- **balance_snapshots** — one per wallet, totals JSON, stable_total Float
- **balance_history** — per-user aggregate USD over time + per-asset totals JSON
- **provider_error_logs** — error_type bucket (rate_limit / auth / network / unknown)
- **arb_alerts** — user-defined spread thresholds, cooldown via `last_triggered_at`
- **plans** — slug, name, price_usd_monthly/annual, portfolio_limit, portfolio_limit_grace, exchange_keys_per_venue, trade_delay_ms, has_portfolio, is_subscription, is_admin_only, features JSON, is_free, is_active, sort_order
- **billing_periods** — slug, label, months, discount_pct, sort_order, is_active
- **promo_codes** — code, discount_pct, **bonus_days**, max_uses, used_count, **per_user_max_uses**, **target_user_id**, applies_to_plan_ids, is_active, expires_at
- **promo_code_usages** — ledger: promo_code_id × user_id × payment_id × discount_pct
- **payments** — CryptoCloud invoice lifecycle (pending → paid / failed / expired / refunded), `final_amount_usd` is the cart total (NOT `amount_usd` which doesn't exist — referral commission uses final_amount_usd, fixed in earlier commits)
- **popups** — title, body, button_text/url, **target_type**, target_user_id, frequency_type, frequency_minutes, is_active
- **popup_dismissals** — per-user dismissal log (anon dismissals are localStorage)
- **app_settings** — key/value JSON for runtime knobs
- **audit_log** — append-only ledger; admin / billing / security actions
- **tg_link_tokens** — sha256 of single-use deep-link tokens (15min TTL)
- **password_reset_tokens**, **email_verify_tokens** — sha256, 1h TTL
- **trade_orders** — every order placement: user_id, wallet_id, position_id, exchange, symbol, side, intent (open|close), status, exchange_order_id, filled_qty, avg_fill_price, fee_usd, error_kind (exchange|internal|user), raw_response JSON
- **trade_positions** — kind (single|pair), pair_kind (long_short|spot_short), leg_a_*, leg_b_*, realized_pnl_usd, entry/exit_spread_pct, opened/closed_externally
- **trade_pair_decisions** — manual paired/unpaired decisions; `leg_a_key` prefix `spot|` for spot legs
- **referral_earnings** — referrer_id, referee_id, payment_id, pct, amount_usd, payout_request_id, **reversed_at**, **reversal_reason**, **reversal_of_id**
- **referral_payout_requests** — user_id, amount_usd, address, status (pending|paid|rejected), note, created_at, resolved_at
- **paper_positions, opportunity_snapshots, exchange_health, anomaly_events, watchlist_items** — Alpha / paper-trading / analytics

### Migrations (newest → oldest, by alembic dependency chain)
Head is `g0a1b2c3d4e5_payment_refund.py`. Linear chain (no merges).

| Revision | Topic |
|---|---|
| `g0a1b2c3d4e5` | **HEAD** — payments.refunded_* + referral_earnings.reversed_*/reversal_of_id |
| `f9a0b1c2d3e4` | referral_payout_requests v2 (status: pending/paid/rejected) |
| `e8f9a0b1c2d3` | users referral fields + referral_earnings + referral_payout_requests |
| `d7e8f9a0b1c2` | trade_orders + trade_positions + trade_pair_decisions |
| `c6d7e8f9a0b1` | users.failed_login_attempts |
| `b5c6d7e8f9a0` | promo_codes.per_user_max_uses + target_user_id |
| `a4b5c6d7e8f9` | users.auto_renew + expiry_notice_last_sent_at |
| `z3a4b5c6d7e8` | promo_codes.bonus_days |
| `y2z3a4b5c6d7` | popup target_type expansion (`all` → `authenticated`/`anonymous`/`everyone`) |
| `x1y2z3a4b5c6` | admin TOTP 2FA columns (`totp_secret_enc`, `totp_verified_at`) |
| `w0x1y2z3a4b5` | Unlim plan + delete test users (cascade) |
| `w9x0y1z2a3b4` | audit_log table |
| `v8w9x0y1z2a3` | pricing rebase: Screener $45, Full $55 |
| `u7v8w9x0y1z2` | billing_periods discount tuning |
| `t6u7v8w9x0y1` | full plan must have has_portfolio=True |
| `s5t6u7v8w9x0` | billing_periods table |
| `r4s5t6u7v8w9` | rename max → platinum + pricing |
| `q3r4s5t6u7v8` | plans + payments + promo_codes + popups + Wallet.is_main |
| `ev1a2b3c4d5f` | email_verify_tokens |
| `pr1a2b3c4d5e` | password_reset_tokens |
| `p2q3r4s5t6u7` | balance_history.totals (per-asset JSON) |
| `o1p2q3r4s5t6` | app_settings table |
| `n0o1p2q3r4s5` | watchlist_items.initial_spread_pct |
| `m9n0o1p2q3r4` | wallet.purpose |
| `l8m9n0o1p2q3` | users.tg_id + tg_link_tokens |
| `k7l8m9n0o1p2` | wallet.can_trade |
| `j6k7l8m9n0o1` | users.tg_chat_id |
| `i5j6k7l8m9n0` | screener Alpha tables (paper_positions, opportunity_snapshots, exchange_health, anomaly_events) |
| `h4i5j6k7l8m9` | users.tg_username + arb_alerts |
| `g3h4i5j6k7l8` | users.plan_id + plan_expires_at |
| `a2b3c4d5e6f7` | tags.user_id |
| `f2a3b4c5d6e7` | balance_history table |
| `e1f2a3b4c5d6` | provider_error_logs |
| `d0e1f2a3b4c5` | users.last_active_at |
| `e5f6a7b8c9d0` | balance_snapshots |
| `c3d4e5f6a7b8` | users.is_blocked + request_count |
| `a1b2c3d4e5f6` | wallets.is_archived |
| `fb0ca8a11562` | users.is_admin |
| `014613d42a04` | initial schema |

---

## API surface

### Auth (`/api/auth`)
| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/auth/register` | — | Public (rate-limited per IP) |
| POST | `/auth/login` | — | Per-account lockout via `login_throttle` |
| POST | `/auth/login/totp` | — | Second-factor for admins |
| POST | `/auth/me/2fa/setup` `/verify` `/disable` | Admin | TOTP CRUD |
| POST | `/auth/logout` | Bearer | Revokes JWT |
| GET | `/auth/me` | Bearer | Profile + plan info; runs `wallet_quota.enforce_for_user` |
| GET | `/auth/tg-bot-username` | — | Returns `TG_BOT_USERNAME` |
| POST | `/auth/tg-login` | — | TG widget auth (HMAC) |
| POST | `/auth/tg-bot-login` | — | Issue deep-link |
| GET | `/auth/tg-bot-login?token=` | — | Bot-login poll (used by `/tg-done.html`) |
| POST | `/auth/password-reset/request` `/confirm` | — | sha256 token, 1h TTL |
| POST | `/auth/email-verify/request` `/confirm` | Bearer / — | sha256 token, 1h TTL |
| POST | `/auth/me/tg-link-token` | Bearer | Issue TG link |
| DELETE | `/auth/me/tg-link` | Bearer | Unlink TG |
| POST | `/auth/me/subscription/cancel` `/resume` | Bearer | Toggle `auto_renew` |
| DELETE | `/auth/me` | Bearer | Account deletion (password + optional TOTP) |

### Wallets (`/api/wallets`)
| Method | Path | Notes |
|---|---|---|
| GET | `/wallets` | List active wallets |
| GET | `/wallets/options` | Dropdown metadata (exchanges, chains, perpdexes) |
| GET | `/wallets/all-addresses` | All addresses across wallets — **declared BEFORE `/{wallet_id}` in routes** |
| POST | `/wallets` | Create wallet |
| GET | `/wallets/archived` | Archived |
| POST | `/wallets/{id}/archive` `/unarchive` | Soft delete + restore |
| PATCH | `/wallets/{id}` | Update name/credentials |
| POST | `/wallets/{id}/main` | Mark as main key (one per user+venue) |
| DELETE | `/wallets/{id}` | Hard delete |
| POST/DELETE | `/wallets/{id}/tags/{tag_id}` | Attach/detach tag |
| GET/POST/DELETE | `/wallets/{id}/addresses[/{address_id}]` | Address book per wallet |

### Portfolio (`/api/portfolio`)
| Method | Path | Notes |
|---|---|---|
| POST | `/portfolio/balance` | Fetch balances; counts toward `request_count` |
| POST | `/portfolio/balance/stream` | SSE streaming variant |
| POST | `/portfolio/transactions` | Per-wallet history |
| POST | `/portfolio/transactions/bulk` | Bulk |
| GET | `/portfolio/history` | Aggregate USD chart |
| GET | `/portfolio/export` | CSV/JSON |

### Screener (`/api/screener`) — REST
| Path | Notes |
|---|---|
| `/screener/funding` | All funding rates (cached 30s, 120/min/IP) |
| `/screener/long-short` | Futures L/S arb (canonical) |
| `/screener/arbitrage` | Legacy alias |
| `/screener/spot-short` | Spot/perp basis (canonical) |
| `/screener/spot-arbitrage` | Legacy alias |
| `/screener/dex-short` | DEX/perp basis (canonical) |
| `/screener/dex-arbitrage` | Legacy alias |
| `/screener/all-arbitrage` | Combined feed |
| `/screener/exchange-health` | Per-venue freshness + status dots |
| `/screener/availability` | Enabled venues + symbols pre-flight |
| `/screener/pair?symbol=&long_ex=&short_ex=` | Single pair |
| `/screener/orderbook?...` | Per-pair OB (Go-fetcher reads via Redis or file) |
| `/screener/orderbooks` | Bulk OB |
| `/screener/orderbook-spot` | Deprecated; spot now flows through unified path |
| `/screener/in-out` | In/out basis (now baked into WS, this endpoint is legacy) |
| `/screener/arb-price-history` | 1h candles |
| `/screener/all-exchanges-funding` | Funding + history |
| `/screener/open-interest` | OI per symbol |
| `/screener/arb-history` | Correlation analysis |

### Screener — WebSocket (all routed via nginx to `go-fetcher:8090`)
- `WS /api/screener/ws/funding` — funding diffs (clients have largely moved to 3s REST poll; WS endpoint still works)
- `WS /api/screener/ws/long-short` — futures arb diffs (canonical)
- `WS /api/screener/ws/arb` — legacy alias
- `WS /api/screener/ws/book` — orderbook diffs, subscribe/unsubscribe protocol

### Trade (`/api/trade`)
| Method | Path | Notes |
|---|---|---|
| GET | `/trade/status` | Trading account status |
| GET | `/trade/positions` | Open + closed |
| GET | `/trade/balances` | Across trading wallets |
| GET | `/trade/orders` | Order history |
| GET | `/trade/pnl` | P&L summary |
| GET | `/trade/spot-short-pairs` | Auto-detected spot↔short pairs |
| POST | `/trade/pair/spot-short/sync` `/unsync` | Manual spot-short pairing |
| GET | `/trade/pair/decisions` | Persisted decisions |
| POST | `/trade/pair/sync` `/unsync` | Generic pair sync |
| GET | `/trade/supported` | Trade-capable venues |
| GET | `/trade/leverage-limits` | Per venue/symbol |
| POST | `/trade/open-arb` `/open` `/close` | Order execution |
| PATCH | `/trade/wallets/{id}` | Update trading wallet |

### Alerts (`/api/alerts`)
| Method | Path | Notes |
|---|---|---|
| GET | `/alerts` | List |
| POST | `/alerts` | Create |
| PATCH | `/alerts/{id}` `/toggle` | Update / enable-disable |
| POST | `/alerts/token` | Token-based alert |
| DELETE | `/alerts/{id}` | Delete |

### Alpha (`/api/alpha`)
Paper trading + analytics. Endpoints under `/alpha/paper/*`, `/alpha/executable-spread`, `/alpha/health`, `/alpha/replay`, `/alpha/leaderboard`, `/alpha/correlation`, `/alpha/anomalies`, `/alpha/backtest`, `/alpha/watchlist`.

### Billing (`/api/billing`)
| Method | Path | Notes |
|---|---|---|
| GET | `/billing/plans` | Public |
| POST | `/billing/payments/checkout` | CryptoCloud invoice |
| POST | `/billing/payments/cryptocloud/webhook` | Sig-verified, refuses if secret unset |
| GET | `/billing/payments/me` | User's payment history |
| POST | `/billing/promo/validate` | Pre-flight validation |
| GET | `/billing/popups/pending` | Auth-optional popup loader |
| POST | `/billing/popups/{id}/dismiss` | Record dismissal |

### Referrals (`/api/referrals`)
- `GET /referrals/me` — Stats: balance / earned / pending
- `POST /referrals/me/payout` — Request payout
- `GET /admin/referrals/...` — Admin management

### Admin (`/api/admin`)
36+ endpoints under `Depends(get_admin_user)`. Notable:
- `GET /admin/stats`, `/admin/users`, `/admin/users/{id}` — overview
- `PATCH /admin/users/{id}/block` `/plan` `/referral-pct` — user mgmt
- `GET/PATCH /admin/screener-config` — disabled symbols/exchanges
- `POST /admin/maintenance` — scope toggles + ETA
- `GET/PATCH /admin/banner` — site banner
- `GET/PATCH /admin/portfolio-config` — portfolio settings
- `GET /admin/funding-ws-health` `/data-plane-health` `/logs` — operational
- Full plan/promo/popup/period CRUD

### Health (`/api/health`)
Public endpoints: `/health`, `/streams`, `/banner`, `/maintenance/status`, `/metrics` (Prometheus), `/providers`, `/feeds`, `/fetcher`.

### Tags (`/api/tags`)
Standard CRUD for user-scoped tags (NULL `user_id` = system tag).

### Meta (`/api/meta`)
- `GET /meta/venues` — public venue manifest used by `frontend/exchanges.js`

---

## Frontend pages

| Page | Route | Auth | Maintenance scope |
|---|---|---|---|
| `index.html` | `/` | public | — |
| `landing.html` | `/landing` | public | — |
| `login.html` | `/login` | public | — |
| `register.html` | `/register` | public | — |
| `tg-done.html` | `/tg-done` | public | — |
| `verify-email.html` | `/verify-email` | public | — |
| `password-reset.html` `password-reset-confirm.html` | `/password-reset[-confirm]` | public | — |
| `pricing.html` | `/pricing` | public | — (stays open during portfolio maint so users can renew) |
| `checkout.html` | `/checkout` | auth | — |
| `screener.html` | `/screener` | public 2-min preview, then signup | screener |
| `arb.html` | `/arb` | public data + authed panels | screener |
| `watchlist.html` | `/watchlist` | auth | screener |
| `portfolio.html` | `/portfolio` | auth | portfolio | (legacy `/app` 301-redirects here) |
| `archive.html` | `/archive` | auth | portfolio |
| `profile.html` | `/profile` | auth | portfolio |
| (no dedicated file) | `/avashare` | auth | portfolio — Avashare/referral UI lives in `profile.html` + `admin.html → Avashare tab`; `/avashare` path is reserved for the maintenance scope but routes to `profile`/`admin`. Standalone `avashare.html` was planned but never shipped |
| `admin.html` | `/admin` | admin | — |
| `admin-user.html` | `/admin-user` | admin | — |
| `404.html` | (fallback) | public | — |
| `maintenance.html` | (served on flag) | public | — |

`serve_page()` in `app.py` enforces redirects: `_AUTH_PAGES = {"portfolio", "profile", "archive", "watchlist"}` redirects to `/login?next=` on missing session; `_ADMIN_PAGES = {"admin", "admin-user"}` additionally check `is_admin` and trip the honeypot on non-admins. `_LEGACY_REDIRECTS = {"app": "/portfolio"}` 301-rewrites old bookmarks. (`/avashare` lives under the auth-protected hierarchy via different gating; not in `_AUTH_PAGES`.)

### Shared JS modules
- **auth.js** — `Auth.{getToken, getUser, setSession, clearSession, isLoggedIn, isAdmin, requireAuth, requireAdmin, redirectIfAuthed, logout, apiFetch}`. `apiFetch` adds Bearer header + auto-redirects 401 → `/login`.
- **toast.js** — `toast(title, type?, sub?)` or `toast({title, sub, type, duration})`. Top-right, 3-4s.
- **theme.js** — applies saved theme ASAP before DOMContentLoaded; toggle button intentionally disabled.
- **navbar.js + navbar.css** — `<app-navbar page="…">` Web Component. Variants per page.
- **popup.js** — `AvalantPopup.reload()`. Polls `/api/popups/pending`, anon-aware (`localStorage.avalant_popup_anon_dismissed`).
- **confirm.js** — `Confirm.ask({…})` / `Confirm.tell({…})`. Modal replacement for `confirm()/alert()`.
- **expiry-banner.js** — sticky banner on auth pages; pings `/api/auth/me`; dismissible 24h via `localStorage.expiry_banner_dismissed_until`.
- **formatters.js** — `FMT.{price, volume, apr, rate, pct, countdown, sign, esc}`. Incrementally adopted.
- **exchanges.js** — `EX.{labels, colors, dot(ex), chip(ex, opts), lists, counts, ready, loadVenues}`. Backfills from `/api/meta/venues`. Single source of truth for exchange palette.
- **anon-gate.js** — screener-only 2-min free preview gate. Timer in `localStorage.anon_first_seen_at`; hard lock after 120s.
- **banner.js** — site-wide announcement bar, polls `/api/banner` every 60s. Static or marquee modes.

### Screener modes
| Tab | Backend | WS | Notes |
|---|---|---|---|
| Long/Short (`arb`) | `/screener/long-short` | `/ws/long-short` | Perp/perp arb |
| Spot/Short (`spot`) | `/screener/spot-short` | (via `/ws/long-short` mux today, may split) | Spot-perp basis |
| DEX/Short (`dex`) | `/screener/dex-short` | (via `/ws/long-short` mux) | DEX-perp basis |
| Funding | `/screener/funding` | none — REST poll every ~3s | Pure funding scan; perf decision after `a4ce086` |
| Alpha | `/screener/watchlist` | `/ws/watchlist` | User's saved pairs |
| All | combined | all of above | Union view |

In/Out columns are now baked into the arb output (commit `5d21070`); per-tick re-render dropped (commit `88f1450`).

### Arb pair page panels
1. Navbar (`<app-navbar page="arb">`)
2. Infobar (hero symbol + exchanges + swap button + pair search popover)
3. Two exchange cards (LONG green, SHORT red): funding rate, interval, next settlement, volume, OI
4. Dual orderbooks (collapsible on mobile)
5. Account block (Positions / Balances / Orders tabs; auth-locked overlay for anon)
6. Trade card (size slider, leverage, P&L calc, executor)
7. Info panel (Greeks, delta input)
8. Share card (snapshot canvas)
9. Alerts modal
10. Sync pairs button + Keys popover

Spot positions auto-render in the per-pair panel for spot/perp arbs (commit `ac655b5`).

### Mobile breakpoints
- Screener: `≤700px` (mobile toolbar), `≤768px` (filter panel), `≤560px` (card width + 7-char ex names)
- Arb: `≤900px` (orderbooks stack), `≤768px` (infobar wraps, status strip hidden, orderbooks collapse default), `≤560px` (chart max-width 480px)

### Exchange palette (`frontend/exchanges.js`)
Full venue → hex map (extract):
- Binance `#F0B90B` · Bybit `#F0842D` · OKX `#C8C8C8` · Gate `#17C684` · KuCoin `#09BA86` · MEXC `#17D854` · Bitget `#00D2C8`
- Hyperliquid `#64B4FF` · Aster `#8A63D2` · Ethereal `#C864C8` · Lighter `#A78BFA` · Paradex `#FF6A6A` · Extended `#E879F9`
- WhiteBIT `#2DCCCD` · BingX `#1DB8F2` · Backpack `#4ADE80` · HTX `#2E7DF6` · Kraken `#7C5CFF`
- Chains: Eth `#627EEA` · BSC `#F3BA2F` · Polygon `#8247E5` · Arbitrum `#28A0F0` · Optimism `#FF0420` · Base `#0052FF` · Avalanche `#E84142` · Tron `#C2A633` · Solana `#9945FF` · zkSync `#1C9BEF` · Linea `#7B61FF` · Scroll `#FFEEDA` · Mantle `#27E5C7` · Blast `#FCFC03` · Fantom `#13B5EC`

### CDN scripts
- `https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js` on `/arb`. CSP `script-src` whitelists `unpkg.com` for this.

---

## Provider system (Python)

`backend/providers/`:

### CEX exchanges (`exchanges/`)
12 venues, all `enabled=True`: binance, bybit, okx, gate, mexc, kucoin, bitget (passphrase), bingx, whitebit, backpack, kraken, htx.

### Perpetual DEXes (`perp_dexes/`)
6 venues, all `enabled=True`: hyperliquid, aster, ethereal, lighter, paradex, extended.

### Chains (`chains/`)
15 chains: 13 EVM (ethereum, bsc, polygon, arbitrum, optimism, base, avalanche, fantom, zksync, linea, scroll, mantle, blast) + tron + solana. Each has native + ERC20-equivalent token support. Disable via `CHAIN_META` `"enabled": False`.

### CEX Screener fetchers (`cex_screeners/`)
Used by `arbitrage_service.FETCHERS` to populate cross-venue funding feed.

### Transaction history (`history/`)
Per-chain + per-CEX transaction adapters. Dispatched by `transaction_service.py`.

### Provider class metadata pattern
```python
class BinanceProvider(BaseWalletProvider):
    name = "BinanceProvider"
    label = "Binance"
    enabled = True
    needs_passphrase = False
    needs_api_key = False
    soon = False
```
`WALLET_OPTIONS` is auto-generated from these. Disable via `enabled = False` on the class. Bitget, OKX, KuCoin set `needs_passphrase=True`.

---

## Background daemons (Python)

| Service | Cadence | Owner replica | Responsibility |
|---|---|---|---|
| `plan_expiry_service` | 10 min | both web | Downgrade expired plans to Free |
| `expiry_notifier_service` | 30 min | both web | TG reminder 30d/7d/1d before expiry |
| `alert_service` | 30s | both web | Spread alerts → TG (per `arb_alerts`) |
| `tg_bot_service` | long-poll | leader (Redis SETNX) | Bot updates handler |
| `reconcile_service` | hourly | both web | Sync `trade_positions` with venue API |

Note: arb compute, orderbook + funding WS, file dumpers — all moved to **go-fetcher**. Python `arbitrage_service.py` / `spot_arbitrage_service.py` / `dex_arbitrage_service.py` only own legacy fallback paths and the `FETCHERS` map for ad-hoc REST queries.

---

## Settings / env vars (Python `backend/settings.py`)

### Required (prod)
| Name | Used for |
|---|---|
| `SECRET_KEY` | JWT signing, TG widget HMAC, Go-fetcher auth |
| `ENCRYPTION_KEY` | Fernet for `wallets.credentials` + `users.totp_secret_enc` |
| `POSTGRES_PASSWORD` | Compose-level secret for DB |

### Optional (with defaults)

**Core**
- `DATABASE_URL` (default `sqlite:///./wallet_monitor.db`)
- `REDIS_URL` (default `redis://redis:6379/0`)
- `ACCESS_TOKEN_EXPIRE_DAYS` (30)
- `ALLOWED_ORIGINS` (CORS, CSV)
- `LOG_LEVEL` (INFO)
- `AVALANT_LOG_FORMAT` (`text` | `json`)
- `AVALANT_LOG_DIR` (default `/var/log/avalant`)
- `AVALANT_COOKIE_SECURE` (1 in prod, 0 for localhost)
- `PUBLIC_BASE_URL` (default `https://avalant.xyz`)

**Tuning (Python — mostly legacy fallback)**
- `AVALANT_REFRESH_INTERVAL` (0.3s — arb compute, legacy)
- `AVALANT_BROADCAST_INTERVAL` (0.2s — broadcast, legacy)
- `AVALANT_ARB_CACHE_TTL` (0.4s)
- `AVALANT_FETCHER_MODE` (legacy `multiprocess` mode)
- `AVALANT_WORKER_EXCHANGES` / `AVALANT_FUNDING_WORKER_EXCHANGES` (CSV filter for orderbook/funding workers)
- `AVALANT_ROLE` (`web`/`fetcher`/`monolith`)
- `AVALANT_RUN_MIGRATIONS` (`true` on app, `false` on app2)

**Go-fetcher cutover**
- `AVALANT_TRADE_PROXY_URL` (default `http://go-fetcher:8090`)
- `AVALANT_INTERNAL_SECRET` (shared secret with go-fetcher)
- `GO_TRADE_VENUES` (CSV list of venues routed to Go; rest stay on Python)

**Telegram bots**
- `TG_BOT_TOKEN` / `TG_BOT_USERNAME` (alerts)
- `TG_AUTH_BOT_TOKEN` / `TG_AUTH_BOT_USERNAME` (login + admin alerts; falls back to TG_BOT_TOKEN)

**Payments**
- `CRYPTOCLOUD_API_KEY`, `CRYPTOCLOUD_SHOP_ID`, `CRYPTOCLOUD_WEBHOOK_SECRET` (refuses with 503 if unset)
- `CRYPTOCLOUD_SUCCESS_URL`, `CRYPTOCLOUD_FAIL_URL`

**Market data**
- `CMC_API_KEY` (CoinMarketCap top-100)
- `ANKR_KEY`, `TATUM_KEY` (RPC providers)
- `TRON_RPC` `TRON_KEY`
- `SOLANA_RPC`
- Per-EVM: `ETHEREUM_RPC`, `BSC_RPC`, `POLYGON_RPC`, `ARBITRUM_RPC`, `OPTIMISM_RPC`, `BASE_RPC`, `AVALANCHE_RPC`, `FANTOM_RPC`, `ZKSYNC_RPC`, `LINEA_RPC`, `SCROLL_RPC`, `MANTLE_RPC`, `BLAST_RPC`

**Per-exchange API base** (override only)
- `BINANCE_BASE_URL`, `OKX_BASE_URL`, `GATE_BASE_URL`, `MEXC_BASE_URL`, `KUCOIN_BASE_URL`, `BYBIT_BASE_URL`, `BITGET_BASE_URL`, `KRAKEN_BASE_URL`, `WHITEBIT_BASE_URL`, `BINGX_BASE_URL`, `HTX_SPOT_BASE_URL`, `HTX_FUTURES_BASE_URL`

**Dev only — never set in prod**
- `AVALANT_AUTH_DEV_EXPOSE_TOKEN=1` — leak password-reset / verify-email tokens in JSON

## Settings / env vars (Go-fetcher)

| Name | Default | Purpose |
|---|---|---|
| `REDIS_URL` | (required) | Pub/sub + key writes |
| `AVALANT_FETCHER_CACHE_DIR` | `/tmp/avalant_cache_go` | Where Go writes cache files (override to `/tmp/avalant_cache` for cutover) |
| `AVALANT_PREWARM_TOP_N` | 20 | Bootstrap symbol count |
| `AVALANT_FILE_DUMP_INTERVAL` | 250ms | books.json / funding.json write cadence |
| `LOG_LEVEL` | WARN | zerolog level |
| `AVALANT_WS_BROADCAST_PORT` | (set in compose, e.g. 8090) | Public WS broadcaster port |
| `SECRET_KEY` | (required if WS port set) | JWT validation |
| `AVALANT_INTERNAL_SECRET` | (optional) | `/internal/trade/*` auth header |
| `AVALANT_BOOTSTRAP_FROM_DIR` | unset | Optional: bootstrap symbols from Python's `funding.json` |
| `AVALANT_WORKER_EXCHANGES` | (empty = all) | Filter orderbook adapters |

---

## Logging

`backend/logging_config.py`:
- Console + rotating files (10MB × 5 per channel = 50MB cap) under `<LOG_DIR>/<role>/{full.log, errors.log}`
- Separate `errors.log` (WARNING+)
- `AVALANT_LOG_FORMAT=json` → JSON output (`JsonFormatter`)
- Uncaught-exception hooks: `sys.excepthook`, `threading.excepthook`, asyncio loop exception handler

Roles: `web` (uvicorn, both replicas), `monolith` (local dev). Go-fetcher logs via zerolog directly to the same `avalant_logs` volume.

Third-party loggers suppressed to WARNING: httpx, httpcore, urllib3, websockets, sqlalchemy.engine, uvicorn.access.

---

## Important gotchas (curated)

1. **`wallet.provider` is a class**, not an instance. Use `wallet.provider()` to instantiate.
2. **Passphrase exchanges**: OKX, KuCoin, Bitget. Indicated by `needs_passphrase = True`.
3. **`credentials` in DB** is Fernet-encrypted. Cache: derive once per process.
4. **`return_exceptions=True`** in `asyncio.gather` — one provider failing doesn't crash others.
5. **First user is NOT admin** — admin is SQL-only on the host. `INITIAL_ADMIN_USERNAME` and `AVALANT_ALLOW_FIRST_USER_ADMIN` env vars are no longer honoured.
6. **SQLite quirk**: use `sa.true()` / `sa.false()` in migrations, not `'true'` strings — SQLite stores literal text and Python treats it as truthy.
7. **bcrypt<5**: passlib 1.7.4 is incompatible with bcrypt 5.x. Pin `bcrypt>=4,<5`.
8. **`postgres://` → `postgresql://`** normalization in `db/base.py` and `alembic/env.py`.
9. **Wallet limit on backend** via `plans.wallet_limit` / `wallet_quota.enforce_for_user`. 402 on exceed.
10. **KuCoin** requires `pageSize >= 10` for deposits/withdrawals.
11. **Bitget** v2 deposit/withdrawal endpoints 404 — use bills.
12. **`/api/wallets/all-addresses` BEFORE `/{wallet_id}`** in route declaration order.
13. **MutableHeaders has no `.pop()`** — use `del headers["key"]` after `if "key" in headers`.
14. **`form.elements['name'].value`**, never `form.name.value` (returns "").
15. **Tag dropdown** rendered as a portal in `document.body` to escape `overflow-y: auto` clipping.
16. **`request_count`** incremented only on `/balance` and `/transactions` endpoints.
17. **Lighter HTTP 400 = unregistered address** (normal), not an error.
18. **`/promo/validate` must pass `user_id`** for per-user / target_user_id enforcement.
19. **Plan-id, not plan-string** is the source of truth. The legacy `users.plan` is a mirror.
20. **Webhook signature must validate** — if `CRYPTOCLOUD_WEBHOOK_SECRET` is empty, the route returns 503. Never fail-open.
21. **fcntl.flock auto-releases** on process death — cache lock orphan-recovery is automatic.
22. **WS auth is first-frame**, never URL `?token=`.
23. **TG bot polling uses Redis lock** — only one replica polls at a time. Without Redis, falls back to no-coordination polling.
24. **Honeypot trips on `/api/admin/*`, `/admin`, `/admin-user`** for non-admins — the user is auto-blocked.
25. **Frontend bind-mounted as `/app/frontend:ro`** — host edits propagate without container restart.
26. **Cache-Control on JS/CSS is 60s**, static images get 86400s (set by `serve_page`).
27. **CSP `unsafe-inline` is required** for now — every HTML ships inline `<script>`/`<style>`. `unpkg.com` whitelisted in script-src for lightweight-charts.
28. **`/api/maintenance/status` is public** so lockout pages can poll-and-reload.
29. **Binance delisted symbols** (e.g. NTRN) keep returning in `/ticker/24hr` and `/premiumIndex` — filter via `/exchangeInfo` `status='TRADING'`. Cached 10 min. Same in Go.
30. **Spot httpx pool is dedicated** in any remaining Python paths — never share with `arbitrage_service._http`.
31. **`auto_renew=False` ≠ plan ended** — plan is active until `plan_expires_at`, just no expiry pings.
32. **Promo bonus_days** add to `activated_until` AFTER the regular billing-period window.
33. **Maintenance ETAs auto-clear when in the past** — `_ends_at()` returns None for stale ISO strings.
34. **`/avashare` and `/api/popups`** are blocked by portfolio maintenance scope. `/pricing` and `/checkout` stay open intentionally so users can renew.
35. **Encryption key rotation**: `python scripts/rotate_encryption_key.py` with `AVALANT_OLD_ENCRYPTION_KEY` + `AVALANT_NEW_ENCRYPTION_KEY` env. Idempotent — re-run after a partial failure tries OLD then NEW per row.
36. **register strips secrets**: `AVALANT_AUTH_DEV_EXPOSE_TOKEN=1` is the only way to get raw password-reset / email-verify tokens back from the API. Default never exposes.
37. **2FA TOTP is open to all users** (admin + non-admin). 8 single-use recovery codes generated at verify-time, bcrypt-hashed in `users.totp_recovery_codes`, shown to user ONCE; regenerate via POST `/auth/me/2fa/recovery-codes/regenerate` (password required). `users.totp_last_used_at` tracked for security visibility on /profile.
38. **Compose env-block must list each var** — a host `.env` entry isn't auto-forwarded; `x-app-env: &app-env` in `docker-compose.yml` enumerates every `${VAR:-default}` it passes into containers.
39. **Per-account login lockout** — 5 failed attempts = lock. Counter on `users.failed_login_attempts`, cleared on successful login.
40. **`payment.amount_usd` does NOT exist** — use `final_amount_usd`. There was a bug where referral commission silently failed because of this.
41. **HL Python+Go signing is byte-parity'd** — don't reorder fields in `orderAction` struct (Go) or insertion order in `place_order` action dict (Python). msgpack output must match.
42. **Paradex untested live** — port is internally consistent but no real testnet order has confirmed it. Keep out of `GO_TRADE_VENUES` until verified.
43. **Lighter trade actions return errZK in Go** — keep `lighter` out of `GO_TRADE_VENUES` so the dispatcher falls through to Python.
44. **Mirror-pair tolerance is 12%** (not 5%) — bumped in `_pnl_can_pair` after UX feedback that 5% missed legitimate pairs.
45. **Anon screener gate** — 2-min preview from first visit (`localStorage.anon_first_seen_at`); user can wipe localStorage to reset.
46. **`app` and `app2` are separate compose services with `build: .` each** — they get separate image tags (`wallet-monitor-app` vs `wallet-monitor-app2`). `docker compose build app` rebuilds only the first; if you skip `app2`, half the traffic keeps running stale code. Always use `./scripts/deploy.sh backend` (it does both with health checks) or explicitly `docker compose build app app2`. We hit this with the alerts service: `app` had the new atomic-claim code, `app2` had the old code → ~5 duplicate TG messages per fire because half the workers raced freely.
48. **OKX orderbook uses public `books` channel** — `books50-l2-tbt` is private (error 60011). Subscribe chunked to 100 symbols/frame. Parser matches only `"channel":"books"`.
49. **Bitget orderbook subscribe: 50 symbols/frame + 200ms delay** — 200-symbol frames trigger error 30002 "Unrecognized request". `SubscribeDelay()` returns 200ms in `bitget/futures.go`.
50. **WS zombie detection**: `lastData` + `subscribedAt` fields in `ws.Runner`. If 5 min pass after first subscribe with no data frames → force reconnect. `lastMsg` alone is insufficient (pong heartbeats keep it alive while subscriptions are silently dead).

47. **Alerts are one-shot: auto-disable after first successful TG send** — see `_claim_alert_for_fire` (`backend/services/alert_service.py`). Atomic SQL UPDATE…WHERE enabled=TRUE serialises the claim across all 8 uvicorn workers (2 replicas × 4 workers). Telegram retries are intentionally OFF (no idempotency key on `sendMessage` → retry creates a duplicate). Fail-closed: if TG errors, the alert stays disabled. User re-enables from the navbar bell popover.

---

## Common workflows for Claude

### "Add a new exchange" (CEX)
1. Create provider in `backend/providers/exchanges/<name>_provider.py` (inherit `BaseWalletProvider`)
2. Set class attrs: `name`, `label`, `enabled=True`, `needs_passphrase`
3. Implement `fetch_balance(wallet) → BalanceResult`
4. Register in `EXCHANGE_PROVIDERS` dict (`backend/providers/exchanges/__init__.py`)
5. Add to `ExchangeType` enum
6. Add `_<name>_txs(creds)` in `transaction_service.py` + wire into dispatcher
7. Optional: add screener fetcher to `arbitrage_service.FETCHERS`
8. Optional: add Python trade adapter under `backend/services/trade_adapters/<name>.py` and remove from `_READONLY`
9. Optional: port to Go under `go-fetcher/internal/trade/<name>/<name>.go`, add tests, blank-import in `cmd/fetcher/main.go`, update `TRADE_PORT.md`
10. Add hex color to `frontend/exchanges.js` if not auto-backfilled

### "Make a runtime change without deploy"
- Plans, promos, popups, billing periods → `/admin → Monetisation`
- Hidden symbols, disabled exchanges, trade-disabled venues → `/admin → Screener`
- Maintenance scope + ETA → `/admin → Maintenance`
- Banner → `/admin → Communications → Banner`
- Expiry-reminder schedule, broadcast → `/admin → Communications`
- User block, plan grant, referral pct override → `/admin → Users`

### "Frontend-only change"
1. Edit `frontend/*.html|js|css`, commit, push
2. On prod: `./scripts/deploy.sh frontend` (just `git pull` — files bind-mounted)
3. Done. Cached browsers refresh JS within 60s

### "Backend-only change"
1. Commit, push
2. On prod: `./scripts/deploy.sh backend` — rolling rebuild app→app2

### "Go-fetcher change"
1. Commit, push
2. On prod: `./scripts/deploy.sh fetcher` — restarts just go-fetcher (10–20s feed re-warm)

### "Migration"
1. `alembic revision -m "..."` locally, write upgrade/downgrade
2. Test on local SQLite + Postgres
3. `./scripts/deploy.sh migrations` on prod (pair with full-site maintenance for breaking schemas)

### "Add an env var"
1. Add to `backend/settings.py` (Pydantic BaseSettings)
2. Add to `docker-compose.yml` `x-app-env:` block (`VAR_NAME: ${VAR_NAME:-default}`)
3. Add to `.env.sample`
4. On prod: append to `.env`, then `docker compose up -d app app2 fetcher` (no rebuild needed — env recreate)

### "Cut over a venue to Go trade"
1. Verify Go adapter's tests pass: `cd go-fetcher && go test ./internal/trade/<venue>/`
2. On prod: `vi /root/wallet-monitor/.env`, append venue to `GO_TRADE_VENUES=binance,bybit,...,<venue>`
3. `docker compose up -d app app2` (no rebuild — env recreate; go-fetcher already has the adapter)
4. Watch Order History for that venue. Any error returned by Go falls back to Python automatically.
5. After 24h clean, remove the venue from Python's `ADAPTERS` and delete the file.

---

## Workflow conventions

### Git
- Conventional commits: `feat:`, `fix:`, `style:`, `perf:`, `refactor:`, `docs:`, `chore:`
- Body: bulleted notable changes
- Trailer: ALWAYS `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`
- Use HEREDOC for multi-line commit messages
- "коммит и пуш" = `git add <specific files> && git commit -m "..." && git push`. Never `git add -A`
- Never `--amend`, `--force-push`, or skip hooks unless explicitly asked
- Stage explicitly — list files on `git add`

### Code style
- No comments unless non-obvious WHY
- No premature abstractions — three similar lines beats a helper
- No defensive code inside trusted boundaries
- No backwards-compat shims for in-development features
- Concise responses — final answers ≤100 words unless explaining architecture

### When user says X, do Y
| User says | Action |
|---|---|
| "коммит и пуш" | Stage specific files, commit with conventional message, push |
| "перезапусти" | kill uvicorn + restart |
| "убери" | Remove the last added thing + its CSS + JS + state |
| "скинь что видно" / "выведи" | Dump raw data in fenced code block |
| "сделай попривлекательнее" | Visual polish pass — spacing, typography, micro-interactions |
| "сухо" | Add restrained polish, not color |
| "[page] тоже" / "везде" | Propagate change across all equivalent pages |
| "помни это" / "сохрани" | Write to auto-memory or propose CLAUDE.md addition |
| "деплой" | `ssh root@avalant.xyz "cd /root/wallet-monitor && ./scripts/deploy.sh"` |

---

## Decisions to NOT revisit (don't touch)

- **Admin is SQL-only**. No env-var path, no client path, no register flow grants admin.
- **`secure=True` on session cookies** in prod. Override only via `AVALANT_COOKIE_SECURE=0` for localhost dev.
- **WS token via first frame**, never URL.
- **TG-login bot replies with a button**, not just a confirmation message — mobile tabs freeze when TG opens.
- **Both bots can run independently** (auth + alerts). Single-bot deployments fall back gracefully.
- **CSRF tokens are intentionally absent** — Bearer auth, not cookie form-post.
- **Spot httpx client is dedicated** in remaining Python paths, never share with arb's `_http`.
- **Price-outlier filter for futures arb is OFF** per user request.
- **Light theme uses `#006B3C` green + `#8B0000` red**. Don't brighten.
- **Theme toggle button is disabled** (`theme.js` early-return). Don't re-enable without asking.
- **Screener has no left panel** — single column, exchanges-popover dropdown.
- **Freshness dots hidden globally**, only re-enabled in Alpha bottom strip.
- **Net/8h on `/arb` excludes price spread** (funding-only). Screener still computes `gross+spread-fees`.
- **Pricing + checkout stay open during portfolio maintenance** so users can renew.
- **`/admin → Maintenance` toggles are runtime**. No deploy needed.
- **Frontend is bind-mounted** — never copy frontend INTO image at build time only; compose mount must persist.
- **No CSRF**, no email comms (Telegram-only), no Stripe (CryptoCloud-only).
- **HL signing is phantom-agent EIP-712** — don't revert to `personal_sign(sha256(...))`. The Python+Go parity tests are load-bearing.
- **Paradex stays out of `GO_TRADE_VENUES` until a live testnet order confirms** — internal-consistency tests aren't enough.
- **Lighter writes route to Python**, not Go (errZK). Don't add `lighter` to `GO_TRADE_VENUES`.
- **In/Out columns are baked into arb WS output**, not separate poll. Don't re-introduce `/in-out` polling.
- **`/ws/funding` REST poll** (3s) replaced WS subscription. Don't re-add the WS path on the client.
- **No frontend build step**. No npm. No bundler. Vanilla HTML/CSS/JS.
- **Risky perf changes go to a feature branch**, not main. Even after revert, isolate the next iteration in `perf/...`.
- **No transient-ban workarounds** (no TTL bumps, no fallback chains for venue rate-limits). Plan for happy path; address bans separately when they hit.

---

## TODO highlights (see TODO.md)

- **Trade**: Limit/stop/TP orders, order history UI polish, partial fills, Lighter ZK signing in Go (CGO bridge or Go-native), Paradex live testnet order verification, position-size calculator
- **Storage HA**: DB+Redis on same host (SPOF), no PG read replica, logs not off-host, no off-site backups, no monthly restore-test
- **Portfolio**: cost-basis FIFO/LIFO, multi-currency, transaction CSV, hardware wallets, fallback price source for low-cap alts
- **Notifications**: only TG; no email, no in-app notification center, only spread alerts (no price-extremum)
- **Performance**: no frontend bundling/minification, no CDN for static, no PostgreSQL read replica
- **Compliance**: no GDPR data-export endpoint, no cookie-consent banner, no ToS/Privacy pages
- **Screener polish** (deferred plan `buzzing-orbiting-meadow.md`): Long/Short rename across URLs + backend routes, dot+label everywhere, palette unify, orderbook canonical-limit sweep (Binance/Bitget/MEXC), all-tab redesign, cold-start blocking wait

ToS / Privacy / GDPR are required for EU-public release.
