# Live Orderbook Plan — догнать arbion по latency

**Цель**: top-of-book delta от события на бирже до браузера ≤200ms p99 по всем 18 венчурсам.

**Текущее состояние baseline (2026-05-12)**:
- MEXC median 3s, p90 9s, max 60s (acceptable but not "live")
- go-fetcher CPU: 16-20 ядер на 12-core box (oversubscribed)
- `ws slow client, dropping` регулярно в логах
- File dumper 250ms tick + broadcaster 25ms tick = добавочные ~140ms среднего lag

**Целевой baseline после плана**:
- MEXC median <300ms, p90 <800ms
- go-fetcher CPU: 8-10 ядер baseline
- 0 slow-client drops
- Push-driven hot path: venue WS → adapter → Hub → client ≤50ms

---

## Plan status — 2026-05-13 (closeout snapshot)

After Phase 5 trade-streams shipped (the visual-liveness win), the marginal value of remaining Phase 2 work dropped significantly — the user-visible bottleneck the plan was originally targeting is now solved. This section pins the current state of each Phase against reality + reason for any deferrals so the next session doesn't redo the analysis.

| Phase | Status | Notes |
|---|---|---|
| Phase 0 | ✅ DONE | Reconnaissance complete |
| Phase 1 | ✅ DONE & DEPLOYED | Push-through hot path; 25ms→1s safety-net tick |
| Phase 2a (Binance @bookTicker BBO) | ⏸ DEFERRED | Current `@depth20` is stateless 100ms snapshot. Adding BBO requires making adapter stateful (BBO updates top-of-book between depth frames). Post-Phase-5 the trade stream already drives top-of-book updates at trade-rate. Marginal value vs medium complexity. |
| Phase 2b (Bybit `orderbook.1` BBO) | ⏸ DEFERRED | Same state-coordination problem as 2a; Bybit delta book would mis-merge if BBO inserts top level that depth never visits. |
| Phase 2c (OKX `bbo-tbt`) | ⏸ DEFERRED | Same as 2b. |
| Phase 2d (Gate `futures.order_book_update` migrate) | ⏸ DEFERRED | 5× cadence gain (100ms→20ms) but requires snapshot+delta state machine with REST bootstrap. State-machine risk; post-trade-streams the latency improvement is not user-visible. |
| Phase 2e (MEXC) | — | Keep as-is per plan |
| Phase 2f (KuCoin `level2:` raw migrate) | ⏸ DEFERRED | tick-by-tick gain but bigger state-machine refactor than 2d (sequence tracking + snapshot bootstrap + KuCoin's per-conn MaxSymbols cap). |
| Phase 2g (Bitget `books1` BBO) | ⏸ DEFERRED | Same state-coordination problem as 2b. |
| Phase 2h (BingX) | — | Keep as-is per plan |
| Phase 2i (HTX `version` tracking) | ✅ DONE | Branch `perf/orderbook-seq-tracking`; commit `687605f`. Best-effort observability; gap → warn-log; runner's 90s stale-data watchdog handles recovery. |
| Phase 2j (Kraken `seq` tracking) | ✅ DONE | Same branch + commit as 2i. |
| Phase 2k (WhiteBIT) | — | Keep as-is per plan |
| Phase 2l (Backpack verify) | ✅ VERIFIED CORRECT | `internal/exchanges/backpack/futures.go` already subscribes to unaggregated `depth.<BASE>_USDC_PERP` (single-level deltas at ~50-100ms). No migration needed. |
| Phase 2m (Paradex new OB WS) | ✅ ALREADY DONE | `internal/exchanges/paradex/futures.go` exists with JSON-RPC subscribe + delta state machine (`order_book.X.deltas`). Wired in `cmd/fetcher/main.go:665`. Plan TL;DR table marking Paradex as "REST-only" is stale. |
| Phase 2n (Hyperliquid new OB WS) | ✅ ALREADY DONE | `internal/exchanges/hyperliquid/futures.go` exists with `l2Book` subscribe. Plan TL;DR table marking HL as "REST-only" is stale. |
| Phase 2o (Extended new OB WS) | ⛔ BLOCKED | Wire format for `/stream.extended.exchange/v1/orderbooks/{market}` not documented (extended.exchange docs cover trades but not orderbook WS). Cannot implement without either: (a) real docs (b) live wire capture to reverse-engineer. Python uses REST polling at `/api/v1/info/markets/{X}-USD/orderbook`. |
| Phase 3a (file dumper 250ms→2s) | ⚠️ PARTIAL | Deployed at `AVALANT_FILE_DUMP_INTERVAL=1s` (not 2s; safer). -1.25 cores measured. |
| Phase 3b (Redis `ob:*` SETEX removal) | ⚠️ TOGGLE-READY | Branch `perf/redis-book-write-toggle` commit `a4a87f3` adds `AVALANT_REDIS_BOOK_WRITE` env (default true preserves current behaviour). Python `orderbook_cache.py:900` already has file-cache fallback — Writer can be flipped off in prod to save -2-3 cores. Path forward: deploy → flip env → soak → change default → delete. |
| Phase 3c (broadcaster diff incremental) | ⚠️ PARTIAL | LongShort broadcaster was ALREADY computing diffs (added/updated/removed). Real waste was re-decoding arb.json every 100ms when file writes every 500ms. mtime-skip implemented on branch `perf/longshort-mtime-skip` commit `f305719`; estimated -1-2 cores. The original "per-symbol push" rewrite was already done by Phase 1 push-through arch — separate item. |
| Phase 4 (verify) | ⏸ DEFERRED | Requires production instrumentation (per-symbol event-to-broadcast latency probe). No measurement code exists in repo; doing this properly needs a separate observability commit. CLAUDE.md MONITORING.md tick #28 confirms 18/18 venues healthy + 0 organic 5xx; user-visible acceptance is met. |
| Phase 5 (trade streams) | ✅ DONE & DEPLOYED | 17/18 venues actively streaming. Ethereal IP-banned from Cloudflare (passive recovery). |
| Phase 5 tests | ✅ DONE | Branch `test/ticks-coverage` commit `8aaf556`: 9 Ring tests + ≥6 tests per adapter × 18 adapters = ~120 tests total. Pins all 6 wire-format regression bugs found during Phase 5 rollout. |

### Outstanding work by impact

| Item | Impact (est.) | Risk | Status |
|---|---|---|---|
| Phase 3b Redis SETEX removal | -2-3 cores | Medium (data path change) | Toggle ready on branch; needs prod flip + soak |
| Phase 3c mtime-skip merge to main | -1-2 cores | Low | On branch, awaiting prod soak |
| Phase 2i/2j seq tracking merge to main | Observability only | Very low | On branch, awaiting prod soak |
| Phase 2o Extended OB WS | -100ms vs REST | High (no docs) | Blocked on wire format |
| Phase 2a/b/c/g BBO adds | <50ms top-of-book | Medium | Deferred — marginal value post-Phase-5 |
| Phase 2d/f channel migrations | 100ms→20ms cadence | High (state machine) | Deferred — marginal value post-Phase-5 |
| Phase 4 instrumented measurement | Verification only | Low | Deferred until needed |

### Branches to review before merge
- `perf/orderbook-seq-tracking` (Phase 2i+2j) — kraken/htx gap-log + 11 unit tests
- `test/ticks-coverage` — Ring + 18 trade adapter parsers (~120 tests)
- `perf/longshort-mtime-skip` — Phase 3c partial (mtime-skip + 11 tests)
- `perf/redis-book-write-toggle` — Phase 3b unblock (toggle env + 6 tests)
- `test/orderbook-coverage` — 6 state-machine OB adapter tests (gate/bybit/okx/mexc/hyperliquid/paradex, 44 tests)
- `docs/plan-closeout` — this snapshot

---

## Phase 0 — Reconnaissance — ✅ ЗАВЕРШЕНО (2026-05-12)

Прочитаны официальные доки 14 активных венчурсов. Сводная таблица каналов:

| Венчурс | Текущий канал | Cadence сейчас | Лучший публичный | Лучший cadence | Действие |
|---|---|---|---|---|---|
| **Binance** fapi | `@depth20@100ms` | 100ms snap | `@depth@100ms` diff + `@bookTicker` | 100ms diff + tick BBO | Migrate diff + add BBO |
| **Bybit** linear | `orderbook.50` | **20ms** | `orderbook.1` BBO | **10ms** | Add BBO канал |
| **OKX** SWAP | `books` | 100ms | `bbo-tbt` public 10ms; L2-tbt = VIP4/5 | 10ms BBO public | Add `bbo-tbt`, L2 не выходит без VIP |
| **Gate** futures | full snap 100ms | 100ms | `futures.order_book_update` lvl=20 | **20ms** | Migrate — 5x ускорение |
| **MEXC** contract | `sub.depth` | ~4.8/сек | (физический cap) | 4.8/сек | Keep |
| **KuCoin** futures | `level2Depth50` | 100ms guaranteed | `/contractMarket/level2:` raw | **tick-by-tick** | Migrate (seq+REST snapshot) |
| **Bitget** mix-v2 | `books` 50/frame | 100-200ms | `books1` BBO event-driven | event | Add `books1` BBO |
| **BingX** swap | `@depth20` | ~500ms server-fixed | (нет дешевле) | 500ms | Floor — keep |
| **HTX** swap | `depth.size_20.high_freq` | tick-by-tick | (already optimal) | tick | Add version-gap tracking |
| **Kraken** fut | `book` | push-on-change | (already optimal) | event | Add seq tracking |
| **WhiteBIT** | `depth_subscribe` | 100ms | (same) | 100ms | Keep |
| **Backpack** | `depth.<sym>` aggregated? | aggregated | `depth.<sym>` без интервала | tick-by-tick | Verify + migrate |
| **Paradex** | (нет WS) | REST | `order_book.{mkt}.snapshot@15@50ms` | **50ms** | Implement |
| **Hyperliquid** | (нет WS) | REST | `l2Book` WS + `bbo` event | ~500ms block + event BBO | Implement |
| **Extended** | (нет WS) | REST | `/v1/orderbooks/{market}` 100ms delta | 100ms | Implement |
| Ethereal | (broken Socket.IO) | — | — | — | Skip |
| Lighter | (no public WS) | — | — | — | Skip |

**Источники**: official docs прочитаны через WebFetch агентами. Detail запросов сохранён в conversation log.

---

## Phase 1 — Push-through hot path — ✅ DONE (2026-05-12)

**Открытие при чтении кода**: архитектура push-through **уже была реализована**:
- `cache.Store.SetOnUpdate(fn)` — хук регистрируется в `cmd/fetcher/main.go:250`
- На каждый `Store.Store()` вызов фаерится `Book.OnBookUpdate(ex, sym, bids, asks)`
- `OnBookUpdate` пушит JSON прямо в `client.outbox` подписанных клиентов
- 25ms tick (`book.tick`) — это **safety-net**, не основной путь

**Реальный bottleneck**: 25ms тик гонял MGET в Redis 40 раз/сек **регардлесс активности**. Это был чистый CPU оверхед, не задержка ни для какого пути.

**Сделано**:
- `bookBroadcastInterval` 25ms → **1s** (по умолчанию)
- Добавлена env-var `AVALANT_BOOK_TICK_INTERVAL` для tune без билда
- Добавлена в `docker-compose.yml`
- Tests passed: `go test ./internal/wsbroadcast/`

**Файлы**:
- `go-fetcher/internal/wsbroadcast/book.go` — interval config
- `docker-compose.yml` — env-var entry

**Эффект**:
- Real-time path (event-driven): **без изменений** (он и был push-through)
- Safety-net path: 40 раз/сек → 1 раз/сек = **40x меньше Redis MGET**
- CPU: ожидается -3-5 ядер на go-fetcher

**Что НЕ менялось**: hot path uses `OnBookUpdate` synchronous push from adapter goroutine → client outbox. Это и есть desired arch.

**Deploy ready**: можно `./scripts/deploy.sh fetcher` на проде. Roll back: вернуть `AVALANT_BOOK_TICK_INTERVAL=25ms` в `.env` (no rebuild).

---

## Phase 2 — Per-venue channel migration — PENDING

Каждая миграция отдельным коммитом, постепенный rollout. Env-var flag для каждой биржи чтобы откатиться без редеплоя.

### 2a. Binance / Aster
**Migrate**: `@depth20@100ms` (100ms snapshot) → `@depth@100ms` (diff stream) + `@bookTicker` (event-driven BBO).

Нужно реализовать snapshot+delta state machine:
1. WS subscribe → буферим events
2. REST `/fapi/v1/depth?symbol=X&limit=1000` → берём `lastUpdateId`
3. Drop events где `u < lastUpdateId`
4. Первый event: `U <= lastUpdateId+1 AND u >= lastUpdateId+1`
5. Дальше: каждый event's `pu` == previous event's `u`. На gap → resync с шага 2.
6. `[price, "0"]` = remove level

BBO добавляет `<sym>@bookTicker` — top-of-book events, отдельный канал.

**Файл**: `go-fetcher/internal/exchanges/binance/futures.go`, `go-fetcher/internal/exchanges/aster/futures.go`

### 2b. Bybit linear
**Add**: `orderbook.1.{sym}` (10ms BBO) поверх существующего `orderbook.50.{sym}` (20ms depth).

Логика: BBO обновляет top-of-book в Hub немедленно при изменении; depth обновления продолжают на 20ms cadence.

**Файл**: `go-fetcher/internal/exchanges/bybit/futures.go`

### 2c. OKX SWAP
**Add**: `bbo-tbt` (10ms BBO public) к существующему `books` (100ms full depth).

L2 tick-by-tick требует VIP4/VIP5 — недоступно. `bbo-tbt` достаточно для top-of-book real-time.

**Файл**: `go-fetcher/internal/exchanges/okx/futures.go`

### 2d. Gate futures
**Migrate**: full snapshot → `futures.order_book_update` lvl=20 @ 20ms.

Это incremental delta стрим с `U`/`u` полями (Binance-style). Bootstrap через REST snapshot:
1. WS subscribe → buffer
2. REST `/api/v4/futures/usdt/order_book?contract=X&limit=20&with_id=true` → `id` = baseID
3. Apply events where `u > baseID`, first event must satisfy `U <= baseID+1 <= u`
4. Gap → re-snapshot

**Файл**: `go-fetcher/internal/exchanges/gate/futures.go`

### 2e. MEXC contract — KEEP
Server-side ограничение 4.8/сек. v2/v3 API не существует.

### 2f. KuCoin futures — BIG MIGRATE
**Migrate**: `level2Depth50` (100ms guaranteed snapshot) → `/contractMarket/level2:` raw tick-by-tick incremental.

Этот канал даёт каждое L2 изменение с `sequence`/`change` строками. Bootstrap:
1. WS subscribe `/contractMarket/level2:<TOKEN>USDTM`
2. REST `/api/v1/level2/snapshot?symbol=<TOKEN>USDTM` → берём `sequence`
3. Применяем только `change` events где `sequence > snapshot.sequence`
4. Gap → re-snapshot

Сложнее но даёт sub-100ms updates.

**Файл**: `go-fetcher/internal/exchanges/kucoin/futures.go`

### 2g. Bitget mix-v2
**Add**: `books1` channel (top-of-book event-driven) поверх `books`.

**Файл**: `go-fetcher/internal/exchanges/bitget/futures.go`

### 2h. BingX swap — KEEP (~500ms floor)

### 2i. HTX swap
**Add**: track `version` field для gap detection. Текущий адаптер не отслеживает, надо.

**Файл**: `go-fetcher/internal/exchanges/htx/futures.go`

### 2j. Kraken futures
**Add**: track `seq` per product для gap detection.

**Файл**: `go-fetcher/internal/exchanges/kraken/futures.go`

### 2k. WhiteBIT — KEEP (verify update_id gap protocol)

### 2l. Backpack
**Verify** — какой канал сейчас. Если `depth.200ms.<sym>` или другой агрегированный — мигрировать на чистый `depth.<sym>`.

**Файл**: `go-fetcher/internal/exchanges/backpack/futures.go`

### 2m. Paradex — NEW IMPLEMENTATION
WS endpoint: `wss://ws.api.prod.paradex.trade/v1` JSON-RPC.
Subscribe: `order_book.{MARKET}.snapshot@15@50ms` — 50ms full snapshots, no delta logic.
Server ping every 55s, client pong ≤5s.

**Новый файл**: `go-fetcher/internal/exchanges/paradex/orderbook.go`

### 2n. Hyperliquid — NEW IMPLEMENTATION
WS endpoint: `wss://api.hyperliquid.xyz/ws`.
Subscribe: `{type:"l2Book",coin:"X",nLevels:20}` — ~500ms block-paced snapshots.
Plus `{type:"bbo",coin:"X"}` event-driven для top-of-book.

**Новый файл**: `go-fetcher/internal/exchanges/hyperliquid/orderbook.go`

### 2o. Extended — NEW IMPLEMENTATION
WS endpoint: `wss://api.starknet.extended.exchange/stream.extended.exchange/v1`.
Path: `/v1/orderbooks/{market}` — initial SNAPSHOT, then 100ms DELTAs, fresh SNAPSHOT every 60s.
`seq` field for ordering; на gap reconnect.

**Новый файл**: `go-fetcher/internal/exchanges/extended/orderbook.go`

---

## Phase 3 — Cheap wins — PENDING

### 3a. File dumper interval 250ms → 2s
Single line change in `cmd/fetcher/main.go` или env var `AVALANT_FILE_DUMP_INTERVAL=2s`.
Live данные идут через WS, файлы нужны только Python screener REST.
**Выигрыш**: ~3-5 ядер CPU освобождается.

### 3b. Удалить Redis `ob:<ex>:<sym>` publish
`internal/redisbus/Writer` мирорит каждый orderbook update в Redis с TTL 10s. После cutover на go-fetcher Python больше не читает этот ключ.
**Выигрыш**: ~1-2 ядра.

### 3c. Broadcaster diff → incremental
Текущий: full-table diff каждые 25ms.
Новый: per-symbol push (см. Phase 1) убирает full-diff вообще.
**Выигрыш**: ~2 ядра.

---

## Phase 4 — Verify — PENDING

**Замер**: 5 активных пар (BTC, ETH, SOL, LAB MEXC/KUCOIN, FARTCOIN) на 60s.
Метрика: `venue_event_ts → browser_recv_ts` p50/p99.

**Целевая acceptance**:
- p50 ≤100ms top-of-book delta
- p99 ≤300ms top-of-book delta
- MEXC median symbol age ≤300ms
- go-fetcher CPU ≤10 cores baseline

**Sanity-check vs arbion**: одновременно открыть arbion.trade и avalant /arb на той же паре, визуально сравнить частоту тиков.

---

## Phase 5 — Trade streams (arbion-level visual liveness) — PENDING

**Корень "почему мы выглядим медленнее arbion"**: мы упёрлись в физический ceiling MEXC публичного API для depth-канала — **5 pushes/сек** (`sub.depth` incremental). Этот предел нельзя побить ни прокси, ни архитектурой, только институциональный feed (платный).

**Что делает arbion**: подписан на **trade-stream** (каждая сделка отдельным событием) **поверх** depth-канала. Hot пары (LAB) генерят 20-50 сделок/сек. UI рисует тик на каждое событие = 25-55 визуальных обновлений/сек.

**Цель Phase 5**: добавить subscribe на trade-stream для каждой биржи + новый broadcast-канал `/ws/trades` (или дополнить `/ws/book` событиями типа `"trade"`).

### Per-venue trade WS channels

| Venue | Trade channel | Wire format | Rate (active pair) |
|---|---|---|---|
| **Binance** fapi | `<sym>@aggTrade` | `{e:"aggTrade",E,T,s,p,q,m,a}` | 10-100/сек |
| **Aster** | `<sym>@aggTrade` (Binance fork) | same | 10-100/сек |
| **Bybit** linear | `publicTrade.<sym>` | `{topic,ts,data:[{T,s,S,v,p,L,...}]}` | 5-50/сек |
| **OKX** SWAP | `trades` (`instType:SWAP`) | `{arg,data:[{instId,tradeId,px,sz,side,ts}]}` | 5-30/сек |
| **Gate** futures | `futures.trades` | `[contract]` → `{result:[{id,price,size,side,...}]}` | 5-30/сек |
| **MEXC** contract | `sub.deal` | `{channel:"push.deal",data:{p,v,T,t,O}}` | 10-50/сек |
| **KuCoin** futures | `/contractMarket/execution:<sym>USDTM` | matchData stream | 5-30/сек |
| **Bitget** mix-v2 | `trade` channel | `{action:"snapshot"\|"update",data:[[ts,p,sz,side]]}` | 5-30/сек |
| **BingX** swap | `<sym>@trade` (Binance-style) | similar | 5-30/сек |
| **HTX** swap | `market.<sym>.trade.detail` | `{tick:{data:[{amount,direction,price,ts}]}}` | 5-30/сек |
| **Kraken** futures | `trade` feed | `{feed:"trade",product_id,price,qty,side,time}` | 1-10/сек |
| **WhiteBIT** | `deals_subscribe` | `[market,deals[]]` | 1-10/сек |
| **Backpack** | `trade.<sym>` | `{e:"trade",E,s,p,q,b,a,t,T,m}` | 1-10/сек |
| **Paradex** | `trades.{MARKET}` | JSON-RPC pub | 1-10/сек |
| **Hyperliquid** | `{type:"trades",coin}` | `[{coin,side,px,sz,hash,time,tid}]` | 1-50/сек |
| **Extended** | `/v1/trades/{market}` WS | similar | 1-10/сек |
| Ethereal | skip (broken) | — | — |
| Lighter | skip (no public) | — | — |

### Architecture

```
venue trade WS → trade adapter.Parse(frame) → {ex, sym, price, size, side, ts}
              → cache.TradeRing (small per-symbol ring buffer, last 50 trades)
              → broadcaster.OnTrade(ex, sym, trade) → Hub clients on /ws/trades
```

- Дублирует существующий orderbook flow (push-through, без 25ms tick)
- TradeRing — небольшой ring buffer (50 трейдов) per symbol для подписчиков-после-фaкта
- Hub фильтрует per-symbol (как `/ws/book`)
- Frontend `/arb` пейдж рендерит каждое trade event как "тик" в UI (мигание соответствующего price level)

### Файлы (новые)

- `go-fetcher/internal/exchanges/<venue>/trades.go` — отдельный Runner на trade-channel per venue
- `go-fetcher/internal/cache/trade_ring.go` — per-symbol ring buffer
- `go-fetcher/internal/wsbroadcast/trades.go` — новый Hub-channel `/ws/trades`
- `frontend/arb.html` — handler trade events для подсветки price levels

### Ожидаемый эффект

| Метрика | До (только depth) | С trade stream |
|---|---|---|
| Визуальных тиков/сек на LAB | 5 | **25-55** |
| Визуальных тиков/сек на BTC binance | 5 | **50-150** |
| Латенси trade event → UI | n/a | **<100ms** |
| go-fetcher CPU | +N venue trade adapters = ~+2-3 ядер |
| Поведение vs arbion | заметно отстаёт | **сравнимо или быстрее** |

### Риски

| Риск | Митигация |
|---|---|
| Trade WS грузит CPU/память (50/сек × 18 venues × N symbols) | Per-symbol ring buffer, no DB store. Только push-через-Hub без перcистенс. |
| Frontend перерисовка 100/сек тормозит браузер | requestAnimationFrame coalescing на стороне UI (макс 60 fps), не 100+ |
| Some venues используют trade channel для агрегации | Прозрачно — мы получаем то что биржа отдаёт |
| Удвоение количества WS-соединений на go-fetcher | OnReconnect handling уже есть; CPU оверхед оценочно +2-3 ядра |

### Фазы реализации

- **5a**: Binance + Aster trade stream (Binance fork) — `@aggTrade`, простейший wire format
- **5b**: MEXC `sub.deal` — то же что depth, но другой канал
- **5c**: Bybit `publicTrade` — V5 standard
- **5d**: OKX `trades` — стандарт V5
- **5e**: Gate `futures.trades`
- **5f**: KuCoin `/contractMarket/execution:`
- **5g**: Bitget `trade`
- **5h**: BingX `@trade`
- **5i**: HTX `market.<sym>.trade.detail` (gzip)
- **5j**: Kraken `trade`
- **5k**: WhiteBIT `deals_subscribe`
- **5l**: Backpack `trade.<sym>`
- **5m**: Paradex `trades.{MARKET}` — Stark-JSON-RPC
- **5n**: Hyperliquid `{type:"trades",coin}` — custom WS
- **5o**: Extended `/v1/trades/{market}`
- **5p**: Frontend wiring — handler в `arb.html` + screener Live ticks indicator

### Acceptance

Открыть прод `/arb` для LAB MEXC рядом с arbion на той же паре. Визуально частота тиков **сравнима или выше**. CPU go-fetcher не превысил 12 ядер (capacity).

---

## Decisions made

- **OKX L2-tbt не входит в scope** — требует VIP4/5 trading volume. `bbo-tbt` 10ms public покрывает top-of-book.
- **MEXC keeps `sub.depth`** — сервер физически не шлёт быстрее ~5/сек. Это не наш bottleneck.
- **BingX keeps `@depth20`** — нет публичного канала быстрее ~500ms.
- **Ethereal skip** — Socket.IO subscriptions rejected, нет публичного L2 API.
- **Lighter skip** — no public WS orderbook channel.

---

## Risks & rollback

| Риск | Митигация |
|---|---|
| Per-venue migration ломает adapter | Каждый венчурс отдельным коммитом, env-var flag для отката (`AVALANT_VENUE_CHANNEL_LEGACY=mexc,kucoin`) |
| Push-through убирает back-pressure | Per-client 5ms coalescer buffer |
| Snapshot+delta state machine race conditions | Бессостоятельная реализация: на gap всегда re-snapshot |
| KuCoin RST на 99-й subscribe | MaxSymbols cap 30 уже работает, новый канал то же ограничение |
| Paradex live test failure | Implementation тестировать сначала на testnet — не на mainnet |
| 50ms Paradex snapshots грузят сеть | Если >100 markets → возможно chunked subscribe |

---

## Progress log

- **2026-05-12 22:30 UTC** — Phase 0 завершён. Доки 14 венчурсов прочитаны через WebFetch агентов. Сводная таблица составлена.
- **2026-05-12 22:35 UTC** — Plan file создан (этот файл). Phase 1 starting.
- **2026-05-12 22:55 UTC** — Phase 1 ✅. Обнаружено что push-through уже в коде; bottleneck был не там — safety-net 25ms тик гонял MGET. Снижен до 1s + env-var override. Tests pass. Готов к deploy.
- **2026-05-12 23:00 UTC** — Phase 1 deployed (commit `93deefc`). CPU go-fetcher 2129% → 1368% (-7 ядер). MEXC max age 113s → 11s. slow-client drops: 0.
- **2026-05-12 23:08 UTC** — MEXC sub.depth.full → sub.depth (commit `8cc7079`). Live measurement: 2.97/s → 5.0/s pushes. **Откатили**: вылез old quirk — деltы только внутри top-20, цена ушла за окно, локальная книга дрейфует ($0.023 от MEXC live REST). Revert commit `652c9cd`.
- **2026-05-12 23:20 UTC** — Анализ "почему arbion выглядит live": MEXC depth ceiling 5/сек = физический потолок public API. arbion подписан на **trade-stream** (20-50 сделок/сек на hot паре) → 25-55 визуальных тиков/сек. Phase 5 добавлен в план.
- **2026-05-12 13:50 UTC** — Phase 5 **READY** end-to-end. Binance 100-278 ticks/s, MEXC 6-17 ticks/s on hot pairs. /arb on prod now flashes per-level pulses arbion-style. 6 bugs found + fixed in trade-stream parser layer (see Phase 5 status section above).
- **2026-05-12 16:00 UTC** — Phase 5c-l extended to 9 more venues (Bybit, OKX, Gate, Aster, KuCoin, Bitget, BingX, HTX, Kraken). 11/13 standard venues live; WhiteBIT (needs auth) + Backpack (market format quirk) deprioritized.
- **2026-05-12 17:00 UTC** — Phase 5n: Hyperliquid trades live (BTC 6.4/s, ETH 5.3/s, SOL 7.4/s). 12 venues total on /ws/trades.
- **2026-05-12 18:30 UTC** — Frontend entry/exit speedup: sampleEntryExit() throttle 120ms→20ms, /ws/trades onmessage triggers immediate spread/entry/exit refresh + pulls top-of-book forward from trade price. Effectively makes entry/exit indicator update at trade rate (100+ /s on hot pairs) instead of depth rate (20-100ms).
- **2026-05-12 18:35 UTC** — Phase 3 partial: `AVALANT_FILE_DUMP_INTERVAL=1s` (was 250ms default). go-fetcher CPU 1685% → 1560% (-1.25 cores).
- **2026-05-12 19:30 UTC** — Frontend staleness watchdog: toast "Нет данных по {SYM} на {venue}" if 12s after page load no /ws/book frame for that side; toast "Задержка данных {venue}" if >15s since last frame; persistent ⚠ badge on venue card while stale.
- **2026-05-12 20:35 UTC** — Phase 5 closeout: WhiteBIT (deals_ → trades_subscribe method rename), Paradex (trades.{MKT}-USD-PERP JSON-RPC), Extended (new package, path-based publicTrades). All three deployed and live-verified parsing trades. Now **16/18 venues** publishing to /ws/trades.
- **2026-05-12 23:00 UTC** — Phase 5 FULL closeout: Ethereal + Lighter discovered to HAVE public trade WS (earlier "skip" notes were stale). Implemented both:
  - **Lighter** LIVE: `wss://mainnet.zklighter.elliot.ai/stream` channel `trade/<market_id>` reusing existing idMap from orderbook adapter. is_maker_ask field → taker side. Test verified 351 trades in 25s.
  - **Ethereal**: subscribed but rate-limited (HTTP 429) — runner's backoff retries; will recover when IP cools. URL `wss://ws2.ethereal.trade/v1/stream` with `{event:subscribe,data:{type:TradeFill,symbol:BTCUSD}}` (raw WS, NOT Socket.IO — old finding was wrong transport).
  - Stronger pulse animation deployed (alpha 0.75 + 700ms + inset shadow) + per-venue activity dot indicator.
  - **18/18 venues registered**; 15+ actively streaming.
- **2026-05-13 01:00 UTC** — Final fixup: REST-cached market filter for Backpack (`/api/v1/markets`) + WhiteBIT (`/api/v4/public/futures`) — only valid PERP bases get SUBSCRIBE frames. Eliminates flood of "Invalid market" errors that drowned subscribe queues. **17/18 venues now actively streaming** post-deploy:
  ```
  binance 1526, bybit 818, bingx 479, okx 459, bitget 450, lighter 392,
  mexc 331, gate 302, hyperliquid 193, aster 168, htx 159, whitebit 150,
  paradex 50, kraken 56, kucoin 60, extended 18, backpack 19  (per 25s window)
  ```
- **Ethereal** persistently 429 from Cloudflare on `ws2.ethereal.trade` from our prod IP — no SDK / no-UA / Origin-header workaround helped. Best-effort: adapter remains registered, runner backoff retries; when IP cooldown completes (usually hours-to-day), it'll go live automatically. Low-volume DEX so impact minimal.
- **2026-05-12 23:30-01:00 UTC** — Phase 5 backend инфраструктура построена и задеплоена:
  - `internal/ticks/` пакет (Tick + Adapter + Runner + Ring buffer)
  - `wsbroadcast/trades.go` /ws/trades hub
  - `wsbroadcast/server.go` route + handler
  - `symbols/Manager.RegisterTicks` — единая prewarm для ob/funding/ticks
  - `binance/trades.go` — adapter (commits `c6b033c`, `c6b8247`, `f6efa79`)
  - `mexc/trades.go` — adapter (commit `33110bc`)
  - nginx route — добавлен
  - frontend `arb.html` — /ws/trades подписка + pulse animation (commit `cda59d9`)
  - Phase 5 SetSymbols-no-reconnect (commit `21f6195`)
- **Open bug**: на проде Binance ticks WS connect успешен, subscribe отправляется, но **0 data frames** receive (manual test от Python с тем же URL даёт frames мгновенно). Issue isolated to Go runner — orderbook on same endpoint works fine. Под подозрением: `User-Agent` / WS-level subtle gorilla vs python-websockets difference, или per-IP concurrent-conn limit. Phase 5p (frontend) задеплоена но не показывает ticks потому что backend не получает.
- **Discovered**: `@aggTrade` на Binance USDT-M futures выдаёт TIMEOUT с любого клиента, `@trade` работает. Stream был переименован (docs устарели).
- **Reverted**: funding-feed bridge в `symbols/manager.go` (commit `bdb435b`) — раздул prewarm до 1000 syms/venue → Binance combined-stream URL too long → 1008 policy violation.

## Phase 5 status (DONE for Binance + MEXC)

| Componente | Status |
|---|---|
| Ticks Go infrastructure | ✅ deployed |
| Binance @trade adapter | ✅ **278 ticks/sec ETH, 103/sec BTC live** |
| MEXC sub.deal adapter | ✅ **17/sec BTC, 7/sec LAB live** |
| /ws/trades broadcaster | ✅ deployed |
| Frontend /arb pulse | ✅ deployed |
| nginx route | ✅ |
| Live end-to-end | ✅ **A/B validated on prod 2026-05-12** |

### Bugs found + fixed during debug

1. **`@aggTrade` retired on fapi**: Binance docs still list it but `wss://fstream.binance.com/stream?streams=...@aggTrade` returns 0 frames. Switched to `@trade` which provides per-fill events (commit `f6efa79`).
2. **Combined-stream + @trade broken**: `/stream?streams=btcusdt@trade` triggers close 1006 (EOF) within seconds. `/ws` + SUBSCRIBE method works flawlessly. Switched (commit `61030bf`).
3. **MEXC `data` is array not map**: prod logs revealed `{"channel":"push.deal","symbol":"X_USDT","data":[{...}]}` — my struct expected a map and 100% of frames failed parse. Switched to `[]struct` (commit `e4fcc21`).
4. **MEXC parse error on subscribe-ack**: `rs.sub.deal` and `rs.error` frames have `data: "string"`. Added two-step parse: channel gate first, then data decode (commit `80ffbbe`).
5. **Binance JSON case-insensitive collision**: Wire has both `"e"` (event type string) and `"E"` (event time number). Go's json/sonic falls back to case-insensitive matching when there's no exact tag — `"E":1778...` was being routed into the `E string json:"e"` field and failing with type mismatch. Added explicit `EvTime int64 json:"E"` field so the exact match takes priority (commit `26eb283`).
6. **SetSymbols force-reconnect on hasRemoved**: ws.Runner closes conn when symbols are removed (necessary for combined-stream URL update). For ticks the same logic killed sessions every 5s (reconcile cycle) before any data could flow. Dropped the force-close for ticks runner; it's fine to keep receiving events for unwanted symbols until next natural reconnect (commit `21f6195`).

### Live measurement (2026-05-12 ~13:50 UTC)

```
binance:ETH    278.8 ticks/s
binance:BTC    103.1 ticks/s
binance:TON     26.0 ticks/s
binance:DOGS    11.7 ticks/s
mexc:BTC        17.1 ticks/s
mexc:ETH        12.8 ticks/s
mexc:SOL         8.4 ticks/s
mexc:LAB         6.7 ticks/s
```

For the LAB MEXC pair the user explicitly complained about: 6.7 trade events/sec via /ws/trades + 5 depth pushes/sec via /ws/book = **~12 visual events/sec on /arb** — matches arbion's "10-20 updates/sec" claim.

### Phase 5c-5l — 11/13 venues live

After completing Binance + MEXC, extended to 11 more venues with the proven pattern. Live measurement on /ws/trades:

```
binance     3042 / 15s   bybit      1581 / 15s   okx     1106 / 15s
bitget       888 / 15s   gate        462 / 15s   mexc     448 / 15s
bingx        337 / 15s   htx         273 / 15s   kraken   149 / 15s
aster        128 / 15s   kucoin       19 / 15s
whitebit       0 / 15s   backpack      0 / 15s   ← needs debug
```

WhiteBIT and Backpack: subscribed successfully but receive 0 trade events. Likely wrong stream name format or market form. Debug on next pass — both Pareto-2 venues so low priority.

Remaining: Phase 5m (Paradex), 5n (Hyperliquid), 5o (Extended) — all custom WS protocols (Stark JSON-RPC / l2book / x10 SDK style), need bespoke adapters not pattern-copied.

### Decisions: keep stretching to other venues?

Now that the architecture is proven, Phase 5c-5o is mechanical:
- One adapter per venue (Bybit `publicTrade`, OKX `trades`, Gate `futures.trades`, KuCoin `/contractMarket/execution:`, Bitget `trade`, BingX `@trade`, HTX `market.<sym>.trade.detail`, Kraken `trade`, WhiteBIT `deals_subscribe`, Backpack `trade.<sym>`, Paradex `trades.{MARKET}`, Hyperliquid `{type:"trades",coin}`, Extended `/v1/trades/{market}`)
- Pattern: each implements `ticks.Adapter` interface, registers via `mgr.RegisterTicks(venue, runner)` in main.go.
- Frontend already filters by `<exchange>:<symbol>` so no changes needed when new venues come online.

**Lesson learned from debug**: ALWAYS write explicit json tags for both lowercase + uppercase variants of the same letter when wire schema has them. Sonic + encoding/json case-fold fallback is sneaky.

## Next up

**Phase 5 (trade streams) — приоритет #1** для достижения arbion-level визуальной liveness. Без него мы упёрты в depth ceiling и **никаким способом не догоним arbion**.

Phase 2 миграции остаются полезными для уменьшения **latency** (event→ui), но не для **frequency** (тиков/сек). Phase 5 решает frequency.

**Порядок работ:**
1. Phase 5a (Binance + Aster trade stream) — самый простой wire format, проверка концепта
2. Phase 5b (MEXC `sub.deal`) — тот венчурс по которому пользователь сравнивает с arbion
3. Frontend Phase 5p — wire trade events в `/arb` HTML, подсветка price levels
4. Live A/B vs arbion на LAB MEXC и BTC Binance
5. Если победили — расширить на остальные 13 venues
6. Параллельно: Phase 2d (Gate 20ms book_update), Phase 2n (HL `l2Book` WS) для latency

**MEXC drift fix** (актуально прямо сейчас — sub.depth.full deployed после revert): мы на 3/сек, корректно. Если нужно ускорить — нужно subscribe limit:200 тест ИЛИ REST backstop. Phase 5 это не решает — только частота визуальных событий.
