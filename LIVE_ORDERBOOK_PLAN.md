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

## Next up

**Не делать Phase 2b (Bybit BBO)** — 20ms→10ms marginal. Bybit `orderbook.1` имеет независимый `u` seq от `orderbook.50`; смержить state нельзя, нужно держать 2 параллельных стейта что усложняет код ради 10ms.

**Не делать Phase 2c (OKX BBO)** — требует merge logic с `books`, сложно для среднего выигрыша.

**Делать Phase 3 cheap CPU wins сначала** — bump file dumper interval до 1-2s. После этого freed CPU будет хватать для Phase 2 миграций без oversub.

**Затем Phase 2d (Gate)** — самый большой простой выигрыш (5x ускорение для тех пар где Gate действительно активный, но channel migration требует REST snapshot bootstrap).

**Затем Phase 2n (Hyperliquid)** — реализовать `l2Book` WS + `bbo`. Это новый код но без legacy interference.
