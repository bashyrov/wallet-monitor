# Avalant — Максимальное ускорение фетчинга стаканов (per-provider)

> Источник истины для исполнителя (агент Claude Code/Cursor или разработчик).
> Цель: поднять частоту обновления стакана на клиенте с ~5/сек до 20–30+/сек, как у конкурентов,
> выжав максимум скорости из КАЖДОГО провайдера.
> Скоуп: пайплайн данных + WS-каналы бирж. Сервер/железо/локация — вне скоупа.
> Рабочий журнал прогресса ведётся в `STATUS.md` (обязателен, см. Раздел 7).

---

## 0. TL;DR

**Симптом:** у конкурентов ~20–30 обновлений/сек, у нас ~5/сек. Причина — наш собственный пайплайн ставит потолок, а не сеть.

**Частота на клиенте = МИНИМУМ из тактов цепочки** (правило слабого звена):
```
канал биржи → cache → Redis throttle (50ms=20/с) → flushLoop (200ms=5/с) → браузер
```
Сейчас связывает `flushLoop` = **5/сек**. Это min()-цепочка — поднимать надо ВСЕ звенья одновременно, иначе потолок просто переезжает на следующее.

**Три рычага, все обязательны:**
1. `flushLoop` 200ms → 33–50ms (снимает потолок 5/с).
2. WS-канал каждой биржи → BBO/top-of-book ≤33ms (снимает потолок источника 10/с).
3. Redis throttle 50ms → ≤33ms или байпас для горячих пар (снимает потолок 20/с).

Плюс: BBO-каналы не только быстрее — они **легче** (1 уровень вместо 20–50), что снижает CPU на парсинге/сортировке/маршале и часто **упрощает адаптер** (нет delta-merge).

---

## 1. Диагностика

### 1.1 Бюджет задержки (latency)
WS-путь: ~200–400ms. REST-путь (`get_orderbook`: Redis-гейт + `_OB_TTL=0.5s` + `POLL=0.5s` + `FIRST_WAIT=0.7s`): 700ms–1.5s. Конкурент на BBO: 20–50ms.

### 1.2 Частота обновлений (updates/sec) — ГЛАВНАЯ МЕТРИКА
Тик N мс = 1000/N обновлений/сек. Текущие потолки:

| Стадия | Параметр | Потолок |
|--------|----------|---------|
| Канал `depth20@100ms` / `books` (100ms) | 100ms | 10/сек |
| BBO-каналы (`bookTicker`/`bbo-tbt`/`books1`) | 10–20ms | 50–100/сек |
| Redis throttle | 50ms | 20/сек |
| **Book `flushLoop`** | **200ms** | **5/сек ← связывает сейчас** |
| Python REST путь | 500ms | 2/сек |

`flushLoop` к тому же **схлопывает** промежуточные тики (`pending[key]` last-write-wins) → не только медленно, но и рвано.

### 1.3 Проверить ПЕРВЫМ ДЕЛОМ
Откуда фронт `/arb` берёт стакан — `/ws/book` (broadcaster, потолок 5/с) или Python REST `/api/screener/orderbook` (потолок 2/с)? Если REST — фикс flushLoop клиент не почувствует, чинить надо REST-путь / переводить фронт на WS. Проверить: фронтовый JS-модуль стакана + nginx-роутинг `:8090` + `wsbroadcast/book.go`.

---

## 2. Боттлнеки пайплайна (общие, не зависят от биржи)

### 2.1 `flushLoop` 200ms → 33–50ms — ГЛАВНЫЙ
**Где:** `go-fetcher/internal/wsbroadcast/book.go`, `Book.flushLoop`, `time.NewTicker(200ms)`.
**Проблема:** жёсткий потолок 5/сек + схлопывание тиков.
**Как:** вынести в env `AVALANT_BOOK_FLUSH_INTERVAL`, дефолт **50ms** (20/сек). Диф уже дешёвый. Откат флагом без редеплоя.

### 2.2 Redis throttle 50ms → ≤33ms / байпас горячих
**Где:** `redisbus/writer.go`, `WriteBook`, `throttle=50ms`.
**Проблема:** потолок 20/сек, апдейты быстрее выбрасываются. Станет binding после смены каналов на BBO (50–100/сек).
**Как:** env `AVALANT_REDIS_WRITE_THROTTLE`, для горячих пар ≤33ms или 0 (байпас), длинный хвост — с троттлом.

### 2.3 Холодный first paint (reconcile 5s)
**Где:** `symbols/manager.go`, reconcile каждые 5s + `Touch`.
**Проблема:** непрогретая пара появляется через 5+ сек.
**Как:** событийный reconcile (триггер сразу при `Touch`), либо шире prewarm.

### 2.4 `hasRemoved` → полный реконнект
**Где:** `ws/runner.go`, `SetSymbols` (`conn.Close()` на любом удалении).
**Проблема:** штормы переподписок с дырами, особенно на медленных в подписке биржах.
**Как:** дельта-отписка через `BuildUnsubscribe([]string)` без закрытия соединения.

### 2.5 Файловый IPC + arbitrage.json 2–5MB
**Где:** `arb/futures.go`, `wsbroadcast/longshort.go`, Python читает файлы.
**Как:** gRPC/shared-memory вместо файлов; домёрджить `perf/longshort-mtime-skip`; опц. бинарный формат.

---

## 3. PER-PROVIDER: максимальный канал для каждой биржи

Это центральная таблица задач. Для каждого провайдера: текущий канал → целевой (самый быстрый) + точная подписка + ожидаемая частота + подводные камни. Каждая смена — за флагом `<VENUE>_USE_BBO`, чтобы катить по одной бирже и A/B-сравнивать.

**Общий приём для BBO-каналов:** они отдают только top-of-book (1 уровень bid + 1 ask). Это значит — НЕТ delta-merge, НЕТ локального book-state, `Parse()` сильно упрощается (просто вынуть best bid/ask). cache.Store и скринеру нужен именно top-of-book, так что глубина не теряется по делу.

### 3.1 CEX

#### binance
- Сейчас: `@depth20@100ms` (URL combined stream), 100ms = 10/сек.
- Надо: **`@bookTicker`** — real-time best bid/ask на каждое изменение.
- Подписка: URL `wss://fstream.binance.com/stream?streams=btcusdt@bookTicker/ethusdt@bookTicker/...`
- Payload: `{u,s,b,B,a,A}` (b/B = bid px/qty, a/A = ask). Merge не нужен.
- Камень: routing-миграция — `@bookTicker` в `/public`, переживёт. (Funding `@markPrice` в `/market` — отдельная проверка, Раздел 4.)
- Ожидаемо: 30–100/сек.

#### okx
- Сейчас: `books`, 100ms = 10/сек.
- Надо: **`bbo-tbt`** — 10ms снапшот top-of-book, ПУБЛИЧНЫЙ (VIP не нужен; VIP только для `*-l2-tbt`).
- Подписка: `{"op":"subscribe","args":[{"channel":"bbo-tbt","instId":"BTC-USDT-SWAP"}]}`
- Камень: снапшот (не delta) → убрать delta-merge для этого канала. Heartbeat `ping`/25s как был.
- Ожидаемо: до 100/сек.

#### bybit
- Сейчас: `orderbook.50`, 20ms = 50/сек (уже неплохо).
- Надо (максимум): **`orderbook.1`** — 10ms, level-1 SNAPSHOT-only (без delta-merge → проще).
- Подписка: `{"op":"subscribe","args":["orderbook.1.BTCUSDT"]}`
- Камень: level-1 шлёт только снапшоты; при отсутствии изменений 3s — повторный снапшот с тем же `u`.
- Ожидаемо: до 100/сек. (Если не хочешь трогать — 50/сек уже достаточно для цели 20–30.)

#### bitget
- Сейчас: `books15`, 150ms (футурсы).
- Надо: **`books1`** — 10–20ms снапшот (20ms на топ-символах BTC/ETH/SOL/...).
- Подписка: `{"op":"subscribe","args":[{"instType":"USDT-FUTURES","channel":"books1","instId":"BTCUSDT"}]}`
- Камень: сохранить чанк 50/frame + 200ms subscribe-delay (иначе error 30002). Снапшот → без merge.
- Ожидаемо: 50–100/сек на топ-символах.

#### gate
- Сейчас: `futures.order_book_update`.
- Надо: **`futures.book_ticker`** — real-time best bid/ask, 10ms.
- Подписка: `{"time":<ts>,"channel":"futures.book_ticker","event":"subscribe","payload":["BTC_USDT"]}`
- Камень: payload `{t,s,b,B,a,A}`-подобный, merge не нужен.
- Ожидаемо: до 100/сек.

#### bingx
- Сейчас: `@depth20`, ~100ms.
- Надо: **`bookTicker`** (есть в Swap API).
- Подписка: `{"id":"...","reqType":"sub","dataType":"BTC-USDT@bookTicker"}`
- Камень: gzip остаётся; текстовый Ping/Pong остаётся; MaxSymbols=100.
- Ожидаемо: event-driven, десятки/сек.

#### htx
- Сейчас: `depth.size_20.high_freq` (high-freq инкремент, неплохо).
- Надо (легче+быстрее): **`market.<symbol>.bbo`** — best bid/ask на изменение.
- Подписка: `{"sub":"market.BTC-USDT.bbo","id":"sub-BTC"}`
- Камень: gzip остаётся; JSON ping/pong (`{"op":"ping","ts":N}` → `pong`).
- Ожидаемо: event-driven BBO.

#### kraken (futures)
- Сейчас: `feed:"book"` — event-driven L2 дельты. УЖЕ ОПТИМАЛЬНО.
- Надо: ОСТАВИТЬ `book`. НЕ переходить на `feed:"ticker"` — он троттлится 1s = 1/сек.
- Камень: читать top-of-book из book feed.

#### mexc (contract)
- Сейчас: `sub.depth.full` (полный снапшот каждый пуш) из-за бага shrinkage на инкременте.
- Надо: проверить инкрементальный механизм, добавленный MEXC в декабре 2025 (мог починить shrinkage). Если ок → инкремент. Альтернатива для BBO — `sub.ticker` (содержит bid1/ask1) — подтвердить формат в доках.
- Статус: **проверить в доках перед сменой**, не менять вслепую.

#### kucoin (futures)
- Сейчас: `level2Depth50` — снапшот, НО `MaxSymbols=50` + 1 sym/frame + 350ms = 350s на 1000 символов, реально покрывает 50.
- Надо: **`/contractMarket/tickerV2:{symbol}`** — real-time BBO (`bestBidPrice`/`bestAskPrice`/`bestBidSize`/`bestAskSize`), пушится на изменение BBO. Это И быстрее (BBO вместо depth50), И обходит логику тяжёлой depth-подписки.
- Подписка: `{"id":<ts>,"type":"subscribe","topic":"/contractMarket/tickerV2:BTCUSDTM","response":true}`
- Камень: подтвердить лимиты подписки tickerV2 (могут отличаться от depth-каналов); если всё ещё упирается — split-connections (Раздел 4.1). Альтернатива при необходимости глубины: `level2Depth5` (100ms, легче depth50).
- Токен через REST до подключения — как был.
- Ожидаемо: real-time BBO, снимает и потолок частоты, и проблему покрытия.

#### whitebit
- Сейчас: `depth_subscribe` с params `[market,100,"0",true]` (100 уровней).
- Надо: запросить top-of-book — `depth_subscribe` с limit `1` (или подтвердить отдельный BBO-канал в доках).
- Статус: **проверить в доках**.

### 3.2 Перп-DEX

#### aster
- Сейчас: depth (форк Binance), 100ms.
- Надо: **`@bookTicker`** — тот же протокол, что Binance (URL combined stream `btcusdt@bookTicker`).
- Камень: всё как у Binance (X-MBX-хедеры, 250ms reconnect-delay, MaxSymbols=200).

#### hyperliquid
- Сейчас: `l2Book` — снапшот на блоке, ≥0.5s = ≤2/сек.
- Надо: **канал `bbo`** — шлётся при изменении BBO на блоке.
- Подписка: `{"method":"subscribe","subscription":{"type":"bbo","coin":"BTC"}}`
- Камень: payload `{coin,time,bbo:[bid,ask]}`; всё ещё привязано к блоку, но без 0.5s-кап на снапшот.
- Ожидаемо: per-block on change (быстрее текущих 2/сек).

#### paradex
- Сейчас: `order_book@15`, 50/100ms.
- Надо: **`bbo.{market}`** — мгновенно при изменении, без троттла.
- Подписка: канал `bbo.BTC-USD-PERP` (через их subscribe-формат).
- Камень: использовать sequence numbers для порядка.

#### backpack
- Сейчас: `depth.<symbol>` (diff-only, event-driven — уже неплохо).
- Надо: **`bookTicker.<symbol>`** — top-of-book (подтвердить точное имя стрима в доках).
- Подписка: `{"method":"SUBSCRIBE","params":["bookTicker.BTC_USDC_PERP"]}`
- Камень: котировка USDC (не USDT); ts в микросекундах (÷1000 для ms).

#### lighter
- Сейчас: `order_book/<market_id>` (event-driven).
- Надо: оставить (event-driven уже ок); подтвердить, есть ли отдельный BBO-канал.
- Камень: market_id через REST до подписки; канал приходит как `order_book:N` (двоеточие).

#### extended
- Сейчас: path-based `/orderbooks` (нет subscribe-фрейма).
- Надо: подтвердить наличие BBO-стрима на их API.
- Камень: WS-ping каждые ≤10s обязателен (иначе `1011`); seq gap detection.

#### ethereal
- Статус: OB-адаптера нет (Socket.IO). Отдельная задача-фича, не скорость.

### 3.3 Сводка целевых каналов

| Биржа | Сейчас | Цель (макс.) | Подтверждено |
|-------|--------|--------------|--------------|
| binance | @depth20@100ms | `@bookTicker` | да |
| okx | books | `bbo-tbt` (публ.) | да |
| bybit | orderbook.50 | `orderbook.1` | да |
| bitget | books15 | `books1` | да |
| gate | order_book_update | `futures.book_ticker` | да |
| bingx | @depth20 | `bookTicker` | да |
| htx | depth.high_freq | `market.<s>.bbo` | да |
| kraken | feed:book | оставить (не ticker) | да |
| kucoin | level2Depth50 | `tickerV2` (BBO) | да |
| mexc | sub.depth.full | инкремент / ticker | проверить |
| whitebit | depth_subscribe 100 | depth limit 1 / BBO | проверить |
| aster | depth | `@bookTicker` | да (форк Binance) |
| hyperliquid | l2Book | `bbo` | да |
| paradex | order_book@15 | `bbo.{market}` | да |
| backpack | depth | `bookTicker.<s>` | проверить имя |
| lighter | order_book/N | оставить | проверить |
| extended | /orderbooks | проверить BBO | проверить |

---

## 4. Не-канальные боттлнеки по площадкам

- **4.1 KuCoin покрытие:** при переходе на tickerV2 проблема depth50-cap уходит для скринера. Если где-то нужна глубина — split-connections (несколько коннектов по 50).
- **4.2 Extended:** WS-ping ≤10s обязателен; resync по seq gap.
- **4.3 MEXC:** проверить, починен ли shrinkage (инкремент, декабрь 2025), прежде чем уходить с `.full`.
- **4.4 Binance funding routing:** `@markPrice` в `/market` мог отвалиться после 2026-04-23 на немаршрутизированном коннекте — проверить, идёт ли фандинг Binance. Стакан (`/public`) не затронут.
- **4.5 Kraken:** НЕ использовать `ticker` (троттл 1s); `book` feed — правильный.

---

## 5. Регрессии: проверять корректность, не только скорость

После каждой смены канала:
- best bid < best ask всегда (нет «перевёрнутого» стакана — частый баг merge delta);
- для BBO-каналов: убедиться, что 1 уровень корректно кладётся в `[]Level` и downstream это переваривает;
- snapshot/delta склейка (где осталась) не теряет уровни;
- reconnect сбрасывает state (`OnReconnect`), нет залипших уровней;
- seq gap → resync, а не молчаливый stale.

Кривой стакан хуже медленного.

---

## 6. Порядок внедрения

**Фаза 1 (общий пайплайн, поднимает потолок для всех):**
1. Проверить фронт `/ws/book` vs REST (1.3).
2. `flushLoop` 200→50ms за флагом (2.1).
3. Redis throttle за флагом (2.2).
4. Событийный reconcile (2.3).
5. Домёрджить `perf/longshort-mtime-skip` (2.5).

**Фаза 2 (per-provider каналы, по одной бирже за флагом, с замером):**
binance → okx → bitget → gate → aster → hyperliquid → paradex → kucoin → htx → bingx → bybit → (mexc/whitebit/backpack/lighter/extended после проверки доков).

**Фаза 3 (архитектура):**
дельта-отписка (2.4) → KuCoin split при необходимости (4.1) → gRPC/shared-memory (2.5) → resync on seq gap.

---

## 7. Как работать (для исполнителя) + STATUS.md

### 7.1 Принцип: измеряй, не угадывай
Единственная метрика — updates/sec на клиенте (цель 20–30+). Латентность на глаз не считается. Любое изменение оценивается замером до/после. Не сдвинуло метрику → потолок переехал на другое звено (min()-цепочка).

### 7.2 Как мерить (три точки)
1. **Клиент (итог):** подключиться к `wss://<host>/api/screener/ws/book`, auth-JWT, подписка на пару, считать фреймы 10s ÷ 10. Без кода: DevTools → Network → WS → счёт сообщений.
2. **Источник биржи (потолок):** временный счётчик в `ws/runner.go` recv-loop (инкремент на `snap != nil`), лог frames/sec по venue каждые 5s.
3. **Выход broadcaster:** счётчик в `Book.flushLoop` — пар за тик и частота тика.
Сравнение трёх чисел показывает связывающее звено.

### 7.3 Протокол: baseline → одно изменение → замер → фиксация
1. Baseline ДО любых правок: клиент+источник для BTC, ETH, одной низколиквидной. В STATUS.md.
2. Менять строго по одному параметру (иначе не понять, что сработало).
3. Замер после каждого, те же пары, записать дельту и где теперь потолок.
4. Зафиксировать в STATUS.md. Регрессия (Раздел 5) — отдельной галкой.
5. Не растёт → потолок переехал, идти на следующее звено.

### 7.4 Сравнение с конкурентом
Тем же методом: страница пары конкурента → DevTools → WS → фреймы/сек по той же паре (BTC) ОДНОВРЕМЕННО (волатильность влияет). Записать число конкурента в STATUS.md как цель.

### 7.5 Откат за флагом
Каждое изменение — за env/фиче-флагом (`AVALANT_BOOK_FLUSH_INTERVAL`, `AVALANT_REDIS_WRITE_THROTTLE`, `<VENUE>_USE_BBO`). Это даёт A/B на проде и мгновенный откат.

### 7.6 STATUS.md — ОБЯЗАТЕЛЕН
Вести `STATUS.md` в корне репо и обновлять после КАЖДОГО изменения. Шаблон с примером — рядом (`STATUS.md`).
**Правило: задача не done без after-замера. «Сделал» без числа = не сделано.**
Для агента: при старте сессии СНАЧАЛА прочитать STATUS.md (что уже сделано, какие замеры), в конце — дописать журнал сессии.

---

## Источники (сверено с доками, 2026)

- OKX: `books` 100ms инкремент, `bbo-tbt` 10ms снапшот публичный, VIP только `*-l2-tbt` — docs-v5.
- Binance Futures: `@bookTicker` real-time, `@depth<lv>@100ms`, routing `/public` после 2026-04-23 — developers.binance.com.
- Bybit v5: `orderbook.1` 10ms (snapshot-only), `orderbook.50` 20ms — bybit-exchange.github.io.
- Bitget: `books15` 150ms, `books1` 10–20ms (топ-символы) — bitget.com/api-doc.
- Gate Futures: `book_ticker` real-time 10ms — gate.com/docs.
- BingX: `bookTicker` в Swap API — bingx docs/SDK.
- HTX/Huobi swap: `market.<symbol>.bbo` best bid/ask на изменение — huobiapi.github.io.
- Kraken Futures: `book` event-driven L2, `ticker` троттл 1s — docs.kraken.com.
- KuCoin Futures: `/contractMarket/tickerV2` real-time BBO (bestBid/AskPrice), `level2Depth5` 100ms — kucoin.com/docs-new.
- Hyperliquid: `l2Book` снапшот на блоке ≥0.5s, `bbo` при изменении на блоке — hyperliquid.gitbook.io.
- Aster: форк Binance API — docs.asterdex.com.
- Paradex: `order_book@15` 50/100ms, `bbo.{market}` event-driven без троттла — docs.paradex.trade.
- MEXC: инкрементальный OB-механизм добавлен в декабре 2025 — mexc.com/api-docs/futures.
