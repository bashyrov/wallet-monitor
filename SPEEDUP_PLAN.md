# Speedup plan — выжать максимум из бирж

Аудит "что можно ускорить" по 4 осям:
1. Получение данных (orderbook/funding/positions/balance)
2. WebSocket connections
3. Открытие/закрытие ордеров
4. Архитектурные изменения

Каждый пункт: **выигрыш** + **усилия** + **риск**.

---

## A. Order placement (САМОЕ ВАЖНОЕ — где юзер чувствует latency)

### A1. 🔥 TLS handshake на каждый order (-100-300мс/order)
**Проблема**: каждый адаптер делает `httpx.AsyncClient(timeout=N) as c:` per call → новый TCP+TLS на каждый запрос. SG→SG ~50мс, SG→US ~200мс TLS.

**Замер**: на каждый POST `/order` = TLS_setup (200мс) + venue_processing (50-100мс) + TLS_teardown (~30мс). Итого 280-330мс. Только venue_processing — ~50-100мс.

**Фикс**: helper `backend/services/trade_adapters/_http.py` уже создан (singleton AsyncClient per host с keepalive=300s). НЕ замержен в адаптеры.

**Миграция**: 16 адаптеров × ~5 mест каждый = ~80 правок. Можно за 2-3 часа делать топ-3 (binance/bybit/okx) — закрываем 80% объёма.

**Выигрыш**: order latency 280-330мс → 80-150мс (-60%). После warmup — мгновенно.

**Усилия**: 2-3ч на топ-3, день на все.
**Риск**: низкий — паттерн стандартный, тесты не должны ломаться.

### A2. Cutover на Go trade engine (-50-200мс/order)
**Проблема**: GO_TRADE_VENUES=paradex — только 1 биржа на Go. Остальные через Python который медленнее на криптографии (sign, EIP-712 особенно).

**Замер**: Hyperliquid EIP-712 sign в Python = ~30-50мс, в Go = ~5-8мс. Aster тоже EIP-712. Paradex Stark = ещё дороже.

**Фикс**: добавить в `.env`: `GO_TRADE_VENUES=paradex,binance,bybit,okx,gate,kucoin,bitget,bingx,kraken,backpack,whitebit,htx,aster,hyperliquid,ethereal`. Go path с persistent connections УЖЕ написан. Просто включить.

**Выигрыш**: 
- Sign latency: -25-45мс на EIP-712 биржах
- HTTP latency: Go использует connection pool по умолчанию = -100-200мс TLS
- Total: -150-250мс на order

**Усилия**: 5 минут (добавить env + restart). Потом 24ч мониторинга что error_kind не растёт.
**Риск**: средний — Go тесты пройдены, но prod-первый ордер на каждой бирже это truth check. Лучше включать по 2-3 биржи за раз.

### A3. WS-based order confirmation вместо polling (-1-5с)
**Проблема**: после `place_order` мы ждём fill через REST polling в `place_open_order`. Sub-optimal — сейчас же есть user-stream WS!

**Замер**: типичный fill <100мс на venue, но мы видим его через 1-10с потому что polling.

**Фикс**: 
1. После send order → return `order_id` сразу
2. WS user-stream уже LIVE на 8 venues и шлёт `ORDER_TRADE_UPDATE` события
3. Frontend показывает "submitted" → ждёт WS push с fill → обновляет статус

**Выигрыш**: видимая latency 1-5с → <100мс на 8 venues.

**Усилия**: 2-3 часа (frontend reactor + backend WS dispatch position_update).
**Риск**: низкий — fallback на existing polling если WS DEGRADED.

### A4. Pre-warm exchange info на startup (-50мс/первый order)
**Проблема**: первый ордер на символе зависает на 200-500мс пока подгружается exchangeInfo (filter, lot_size, multiplier).

**Замер**: Binance `/fapi/v1/exchangeInfo` ~150мс, KuCoin `/api/v1/contracts/active` ~100мс. На каждой бирже по разному.

**Фикс**: при startup async fetch всех exchangeInfo сразу. Cache TTL 1ч.

**Выигрыш**: первый ордер на символе -200-400мс. Последующие уже cached.

**Усилия**: 1-2 часа.
**Риск**: низкий.

### A5. Hedge-mode + leverage pre-warm (-1 RTT)
**Проблема**: первый ордер вызывает `set_leverage` (1 RTT, ~80мс) даже если уже выставлен.

**Замер**: уже есть `_state_cache` который пропускает идемпотентные `set_leverage`. Но ПЕРВЫЙ вызов всегда платит.

**Фикс**: при подключении user-stream автоматически читать текущий leverage у позиций → pre-fill state cache.

**Выигрыш**: -50-80мс на первом ордере на символе.

**Усилия**: 1ч.
**Риск**: низкий.

---

## B. Orderbook / WS data freshness

### B1. ✅ Уже сделано
- WS subscribe на 16 venues (Go-fetcher)
- 250мс broadcast cadence
- 2с REST backstop как safety
- inOutStickyTTL 2с (было 8с)
- EvictStale 30мин

### B2. Adaptive REST backstop (-30% CPU/IO)
**Проблема**: REST backstop крутится каждые 2с на каждой бирже даже когда WS идеально работает. Лишние HTTP calls.

**Фикс**: смотреть UpdatedAt — если WS обновился за последние 5с, скипать backstop. Если нет — крутить как сейчас.

**Выигрыш**: -30% backstop HTTP traffic, -20% go-fetcher CPU. Не latency но scaling.

**Усилия**: 30 минут.
**Риск**: низкий.

### B3. HTTP/2 на REST backstops (-50% latency на parallel calls)
**Проблема**: backstop часто делает 5-10 parallel REST calls. С HTTP/1.1 каждый отдельный TCP connection.

**Фикс**: в Go fetcher использовать `http2.Transport`. Один TCP connection, multiplexed streams.

**Выигрыш**: parallel REST -30-50% latency, особенно для венчуров с per-symbol fetch.

**Усилия**: 1ч (добавить http2 transport, тестировать).
**Риск**: средний — некоторые биржи могут не поддерживать HTTP/2 на их API.

### B4. WS depth.20 → depth.1 (для arb compute)
**Проблема**: некоторые orderbook adapters качают depth.20 или depth.50 (20-50 уровней). Для in/out compute нужен только TOP-of-book.

**Фикс**: уменьшить depth до 1-5 уровней где venue поддерживает. Меньше bandwidth.

**Выигрыш**: -60-80% bandwidth на orderbook stream, чуть меньше CPU на parse.

**Усилия**: 30 мин (изменить subscribe params в каждом venue адаптере).
**Риск**: низкий, но frontend orderbook detail page использует более глубокий стакан — нужен проверить что не сломаем.

### B5. Закрыть пробелы покрытия (HL=183 of 200, Backpack=75 of 120, etc)
**Из тика #11 monitoring**: некоторые funding-адаптеры берут подмножество символов биржи.

**Фикс**: каждый funding-адаптер должен дёрнуть venue universe endpoint и subscribe на ВСЕ символы. Не только то что было в предыдущем `arbitrage.json`.

**Выигрыш**: +5-15% покрытия пар на скринере.

**Усилия**: 1ч на адаптер × 4 адаптера (HL/Backpack/HTX/Paradex).
**Риск**: средний — может вылезти WS rate limit на больших sub'ах.

---

## C. Position / Balance fetching

### C1. ✅ Уже сделано
- 8 venues на user-stream WS push (<100мс freshness)
- Snapshot mirror в Redis для cross-replica reads
- Reconcile worker запущен (5min cycle)

### C2. Position fetch ТОЛЬКО из snapshot (-200-500мс)
**Проблема**: `list_user_positions` сейчас читает snapshot, но если snapshot пустой (cold start, до первого WS event) — fallback на REST.

**Замер**: сейчас REST fallback может ждать N секунд если venue медленный.

**Фикс**: 
1. На startup после WS connect — обязательно сделать REST seed (уже делается, см `_seed_from_rest`)
2. После seed — НИКОГДА не fallback на REST. Если snapshot пустой → значит позиций нет.
3. Reconcile worker ВСЁ ЕЩЁ полит REST раз в 5мин для catch external changes.

**Выигрыш**: cold-start latency 1-3с → ~200мс (REST seed). Live freshness уже <100мс.

**Усилия**: 30 мин (rollout flag в trade_service).
**Риск**: низкий — fallback всё равно сработает когда WS DEAD.

### C3. Migrate REST-fallback adapters на user-stream (5 venues)
**Сейчас на REST polling**: backpack, lighter, whitebit, kraken, paradex, hyperliquid (6 venues — у них user-stream адаптеры есть но не подключаются — DEAD/INIT)

**Замер**: эти 6 venues пользователь видит с 10с polling lag.

**Фикс**: проверить почему `_supervisor` не подключает их. Скорее всего:
- Backpack: код есть, могут быть creds-issues
- Lighter: тоже
- WhiteBIT: docs неоднозначны
- Kraken: futures WS auth
- Paradex: Stark sign
- Hyperliquid: WS user channel

**Выигрыш**: эти 6 venues тоже получают <100мс freshness вместо 10с.

**Усилия**: 2-3ч на venue × 6 = 1-2 рабочих дня.
**Риск**: средний — каждый venue это отдельная WS-схема.

### C4. Push balance на каждый fill event (-1 cycle)
**Проблема**: balance не пушится отдельно — приходит только в составе ORDER_TRADE_UPDATE. Если юзер сделал deposit/withdraw, видим только при следующем reconcile (~5мин).

**Фикс**: каждое venue имеет push.balance / wallet.update / asset.update event. В адаптерах parse_event иногда не разбирает их.

**Выигрыш**: balance свежий <100мс после deposit/withdraw на venue.

**Усилия**: 30 мин на venue × 8 venues = ~4ч.
**Риск**: низкий.

---

## D. Архитектура (большие изменения)

### D1. Persistent connections везде (-50% Python CPU)
**Проблема**: каждый async function в Python адаптерах делает `async with httpx.AsyncClient()` → context manager закрывает client сразу. ZERO reuse между вызовами.

**Фикс**: 
1. Глобальный `httpx.AsyncClient` per host (создан в `_http.py`)
2. `async with` заменить на `client = http_client(BASE)` + `await client.get(...)`
3. Закрытие на shutdown через `aclose_all()`

**Выигрыш**:
- TLS handshake: 1× per process per host (~50-300мс) вместо per-call
- TCP slow start: уже разогнан после первых 100КБ
- Memory: -30% (нет тысяч client объектов)

**Усилия**: 1 день, методичная миграция.
**Риск**: средний — connection leaks если не закрывать правильно.

### D2. Order placement через Go ВЕЗДЕ (-100-300мс)
**Сейчас**: GO_TRADE_VENUES=paradex (только 1).

**Что в Go уже работает (полные адаптеры с тестами)**:
binance, bybit, okx, gate, mexc, kucoin, bitget, bingx, htx, aster, kraken, backpack, whitebit, hyperliquid, ethereal, paradex (16/17). Lighter — read-only в Go.

**Фикс**: `GO_TRADE_VENUES=binance,bybit,okx,gate,mexc,kucoin,bitget,bingx,htx,aster,kraken,backpack,whitebit,hyperliquid,ethereal,paradex` (всё кроме lighter).

**Выигрыш на order**:
- HMAC sign: Python ~5мс vs Go ~0.5мс
- EIP-712 sign: Python ~50мс vs Go ~5мс
- TLS handshake: Go uses persistent client = 0мс after warmup
- HTTP/2 multiplexing: Go-default, Python limited

**Усилия**: 5 минут, then 24h prod mon.
**Риск**: SREDNI — это переход на новый stack. У каждой биржи возможны subtle differences. Делать batch'ами по 2-3 venues.

### D3. WS broadcaster на стороне Go (уже есть) — но используется ли?
**Проверить**: фронтенд /screener использует /ws/long-short и /ws/funding. Эти endpoints на go-fetcher:8090. Если nginx правильно проксирует — то всё ок.

**Фикс если нет**: убедиться что nginx routes WS правильно.

**Выигрыш**: уже работает, просто верификация.

### D4. Колонка `next_funding_time` через WS (-2с per row)
**Проблема**: в funding.json `next_funding_time` обновляется через 2с REST backstop. Юзер на /screener видит "2:25" вместо "2:23".

**Фикс**: некоторые venues пушат next_ts в WS. Использовать.

**Выигрыш**: precision freshness +1-2с.

**Усилия**: 30 мин, но lower priority.
**Риск**: низкий.

---

## E. Quick wins (1-2 часа каждый, всё в сумме = большое улучшение)

| # | Что | Выигрыш | Время |
|---|---|---|---|
| 1 | `GO_TRADE_VENUES` 1 → 15 venues | -100-300мс/order | 5 мин + 24h mon |
| 2 | Helper `_http.py` миграция top-3 (binance/bybit/okx) | -100-200мс/Python order | 2ч |
| 3 | Pre-warm exchange info | -200-400мс на первом order | 1-2ч |
| 4 | WS-based order confirmation | UI feels 1-5с быстрее | 2-3ч |
| 5 | Adaptive REST backstop | -30% go-fetcher CPU | 30 мин |
| 6 | Position-fetch только из snapshot (no REST fallback) | -1-3с cold-start | 30 мин |

**Если только эти 6** = ~10-12 часов работы → -200-500мс на order, -1-3с на cold start, лучше UX.

---

## F. Длительные эпики (1-3 дня)

| # | Что | Выигрыш |
|---|---|---|
| 1 | Migrate 5 REST-fallback venues на user-stream WS | 6 venues с 10с polling → <100мс push |
| 2 | Адаптеры под shared `_http.py` (все 16) | -50% Python CPU, -all TLS handshakes |
| 3 | Ethereal orderbook adapter (Socket.IO) | +1 venue на скринере |
| 4 | Funding-coverage аудит и фикс | +5-15% символов на скринере |

---

## Что я бы сделал ПЕРВЫМ

1. **A1 + D2 в одном заходе** — добавить все 15 venues в `GO_TRADE_VENUES` и миграция Python helpers. Это закроет latency на open_position сразу.
2. **A3** — WS confirmation. Юзер реально это чувствует.
3. **C3** (5 venues на user-stream) — закроет последние 6 venues которые сейчас polling.
4. **B5** (funding coverage) — полнота данных.

Готов делать любую комбинацию.
