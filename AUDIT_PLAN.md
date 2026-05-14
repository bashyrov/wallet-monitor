# Avalant — Аудит функционала и план фиксов

Создан 2026-05-14 после комплексного аудита. Не индексирован git (.gitignore при необходимости).

Принцип приоритезации: **Effort × Impact / Risk**. Пункты 1-10 ранжированы.

---

## #1. 🔥 Cache snapshot для `/api/screener/funding` + `/exchange-health`

**Проблема**: server-side latency
- `/api/screener/funding`: **702ms** (должно быть <50ms)
- `/api/screener/exchange-health`: **1440ms** (должно быть <50ms)
- `/api/screener/long-short`: 172ms (приемлемо, но можно лучше)

**Причина**: каждый запрос делает:
1. JSON.parse 1MB файла funding.json
2. Filter loop через 5500 строк
3. cross-listed compute (defaultdict + 2x проход)
4. Возвращает результат

Это compute на КАЖДЫЙ request. Файл обновляется раз в 250ms, а запросов в секунду много.

**Решение**: in-process cache в `arbitrage_service.py`:
- Hold processed snapshot in memory с TTL = 1s
- Refresh background async (не блокирует request)
- Все запросы в этом окне получают готовый response

**Implementation**:
1. В `get_funding_data()` добавить class-level cache: `_funding_snapshot_cache = {"data": None, "ts": 0.0}`
2. Если cache.ts + TTL > now → return cache.data instantly
3. Иначе compute + update cache + return

Аналогично для `exchange_health()` функции (которую вызывает `/exchange-health`).

**Tests**: existing endpoint tests should pass. Add timing assertion: `<100ms` server-side.

**Risk**: низкий. Cache invalidation очевидная (TTL).

**Files**:
- `backend/services/arbitrage_service.py:get_funding_data()` — добавить snapshot cache
- `backend/services/arbitrage_service.py:exchange_health()` — то же
- `backend/api/v1/screener.py:funding_rates()` — без изменений

**Expected**: server-side latency 702ms → ~5ms (hit), 50ms (miss). Total /funding 2.1s → ~600ms (CF + сеть остаются).

**Effort**: 1-2 часа.

---

## #2. 🔥 Debug funding WS — почему `ws_row_count=0` везде

**Проблема**: все 18 venues funding feeds работают через REST. WS неактивен.

`curl /api/health/feeds`:
```
binance      total=565 ws_row=0 rest_row=565 ws_age=None rest_age=0.99 via=rest
... (одинаково для всех 18)
```

CLAUDE.md обещает sub-second funding via WS, по факту 2s polling.

**Возможные причины**:
1. Go-fetcher WS адаптеры подключаются, но `ParseWS` возвращает []Tick{} (silent)
2. Health endpoint Python читает stale `funding_ws.json` который Go-fetcher не пишет
3. Manager.SetSymbols() передаёт пустой список → WS не subscribe
4. WS connection-level ошибка (handshake fail на проде, IP blocking)

**Diagnostic steps**:
1. ssh prod → `docker exec wallet-monitor-go-fetcher-1 sh -c 'cat /tmp/avalant_cache_go/funding_ws.json'`
2. Если файла нет / пустой → Go не пишет funding_ws.json (legacy метрика)
3. Если файл есть и заполнен → Python health endpoint читает не оттуда
4. Если pусто на обеих сторонах → WS реально мёртв

**Likely fix**:
- Если "Python reads stale legacy file" — переименовать API endpoint metric из `ws_row_count` в `live_row_count` reading from go-fetcher in-memory state. ws_row_count может быть deprecated metric.
- Если "WS реально мёртв" — debug per-venue, начать с Binance (самый важный).

**Effort**: 2-4 часа.

**Risk**: низкий. Debug-only initially.

**Expected**: subsecond funding updates на всех 12 WS-capable venues.

---

## #3. ⚡ TLS singleton + httpx keepalive для trade adapters

**Проблема**: каждый Python trade order создаёт новый AsyncClient → TLS handshake 200ms+ каждый раз.

Из SPEEDUP_PLAN.md:
> per-call AsyncClient → новый TCP+TLS на каждый запрос. SG→SG ~50мс, SG→US ~200мс TLS. Итого 280-330ms total. Только venue_processing — ~50-100ms.

**Решение**: 
- helper `backend/services/trade_adapters/_http.py` уже создан (singleton AsyncClient per host с keepalive=300s)
- НЕ замержен в адаптеры

**Implementation**:
1. Проверить что `_http.py` существует и корректен
2. Топ-3 venues: Binance, Bybit, OKX — переписать на singleton (~80 правок)
3. Tests existing pass, observe order latency drop

**Effort**: 2-3ч на топ-3, день на все 16.

**Risk**: низкий — паттерн стандартный.

**Expected**: order latency 280-330ms → 80-150ms (-60%).

---

## #4. ⚡ Go-cutover для всех trade venues

**Проблема**: `GO_TRADE_VENUES=paradex` only. 16 venues по-прежнему через Python (slower crypto, slower HTTP, slower sign).

EIP-712 signing в Python ~30-50ms vs Go ~5-8ms.

**Implementation**:
1. На проде: `vi .env`, append: `GO_TRADE_VENUES=paradex,binance,bybit,okx,gate,kucoin,bitget,bingx,kraken,backpack,whitebit,htx,aster,hyperliquid,ethereal`
2. `docker compose up -d app app2` (env recreate, no rebuild)
3. Monitor Order History 24h. Любая ошибка от Go → автоматический fallback на Python.

**Effort**: 5 мин deploy + 24h мониторинга.

**Risk**: средний — каждый venue должен быть smoke-tested. Внедрять по 2-3 за раз.

**Expected**: -150-250ms per order.

---

## #5. KuCoin REST polling 500ms — реальные bid/ask

**Проблема**: KuCoin Classic WS rotates A@45s / B@55s → 2-сек дыры. Только top-80 pairs работают.

Pairs LONG=any SHORT=kucoin показывают **mark price вместо ask/bid** для Live Spread.

**Решение**: REST polling 500ms для top-N KuCoin pairs which user is interested in:
- `/api/v1/contracts/{symbol}/ticker` per-symbol
- Concurrent fetch, sem=16
- Update `books.kucoin.json` каждые 500ms

**Effort**: 2-3ч.

**Risk**: низкий. Worst case — KuCoin rate limits, deal with it.

**Expected**: реальные bid/ask на KuCoin pairs → правильный Live Spread, не "mark-based".

---

## #6. WS-based order confirmation вместо REST polling

**Проблема**: после `place_order` мы ждём fill через REST polling. Видимая latency 1-10s.

Из SPEEDUP_PLAN.md:
> after send order → return order_id сразу; WS user-stream УЖЕ LIVE на 8 venues и шлёт ORDER_TRADE_UPDATE; frontend показывает "submitted" → ждёт WS push с fill → обновляет статус.

**Implementation**:
1. Backend: на `place_open_order` возвращать order_id сразу + WS push fill
2. Frontend `arb.js`: show "Submitted" → listen на WS для конкретного order_id → update to "Filled"
3. Fallback: existing REST polling если WS DEGRADED

**Effort**: 2-3 часа.

**Risk**: низкий — fallback существует.

**Expected**: видимая latency 1-5s → <100ms.

---

## #7. Arb compute → multiprocessing

**Проблема**: `_compute_arb_sync` (0.3-1s) + alpha + write в одном Python потоке → sustainable cycle 7s.

**Implementation**:
1. multiprocessing.Pool(2)
2. Worker процесс выполняет compute, master шлёт data в pool, забирает результат
3. Master process пишет файл и broadcasts через WS

**Effort**: 4-6 часов (архитектурная работа).

**Risk**: высокий — multiprocessing синхронизация, потенциальные deadlocks.

**Expected**: arb cycle 7s → 500ms-1s.

---

## #8. SPA-style pair navigation на /arb

**Проблема**: BTC→ETH→SOL — каждый раз новый HTML 72KB.

**Implementation**:
1. Single arb-shell.html (минимальный)
2. JSON config endpoint `/api/screener/pair-config?sym=BTC&long=binance&short=bybit`
3. Frontend: history.pushState на смену pair, reload JS state из JSON config
4. WS subscriptions переключаются programmatically

**Effort**: 4-6 часов.

**Risk**: высокий — большой rewrite arb-page state management.

**Expected**: пара-навигация 1-2s → <100ms.

---

## #9. Grafana + Prometheus exporter

**Проблема**: нет proactive monitoring. Регрессии выявляются по жалобам.

**Implementation**:
1. `prometheus-fastapi-instrumentator` для Python (`/metrics` endpoint уже есть, см. CLAUDE.md)
2. Custom metrics: per-venue funding row_count + age, arb opp_count, API p50/p95/p99
3. Grafana dashboard (self-hosted single-node)
4. Alerts на per-venue age > 30s

**Effort**: 8 часов.

**Risk**: низкий.

**Expected**: SLA visibility + faster regression detection.

---

## #10. /portfolio balance auto-refresh

**Проблема**: balances обновляются только на F5. Юзер открыл 5 мин назад → видит stale.

**Implementation**:
1. setInterval 30s на /api/portfolio/balance
2. Gate на document.hidden
3. Visibilitychange wake-up

**Effort**: 1-2 часа.

**Risk**: низкий.

**Expected**: live balance без manual refresh.

---

## Defer / отложено

### #11. KuCoin v2 WS migration — большой refactor, low ROI vs #5
### #12. Ethereal full перфоменс — ждём Cloudflare whitelist (не наш контроль)
### #13. Trade reconcile worker — hourly → 5min cycle — quality-of-life
### #14. /alerts history — feature work
### #15. /pnl backfill — async pull background (now sync 30+ sec)
### #16. Anon gate — incognito bypass (security)
### #17. Mobile bottom-nav overlap — cosmetic
### #18. Skeleton loaders на orderbook — cosmetic
### #19. Toast informative messages — UX polish
### #20. PgBouncer pool monitor — preventive

---

## Outcome metrics (после применения #1-#6)

| Metric | Сейчас | Target | Method |
|---|---|---|---|
| /api/screener/funding server time | 702ms | <50ms | snapshot cache (#1) |
| /api/screener/exchange-health server time | 1440ms | <50ms | snapshot cache (#1) |
| Funding update latency | 2s (REST only) | <500ms (WS live) | #2 |
| Order placement latency | 1-5s | <500ms | #3+#4+#6 |
| KuCoin Live Spread | mark price | real ask/bid | #5 |
| Arb compute cycle | ~7s | ~500ms | #7 (deferred) |
| /arb pair navigation | 1-2s | <100ms | #8 (deferred) |

После #1-#6 (priority quick wins, ~15ч суммарно):
- 90% perf regressions устранены
- Trading experience: order → fill confirm <500ms
- Screener: real-time updates без 2s polling delay
- API endpoints: 10-30x faster

Risk profile сохраняется низким — каждый пункт self-contained, легко rollback.
