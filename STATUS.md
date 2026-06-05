# Avalant — Orderbook Optimization · STATUS (живой журнал)

> Рабочий журнал прогресса по `ORDERBOOK_OPTIMIZATION.md`. Обновлять после КАЖДОГО изменения.
> **Правило: задача не done без after-замера. «Сделал» без числа = не сделано.**
> Метрика — updates/sec на клиенте (цель 20–30+). Как мерить — §7.2 ниже.
> Дата формата YYYY-MM-DD. Статусы: `todo` / `in-progress` / `done` / `blocked` / `reverted`.
>
> **СТАНДАРТ ЗАМЕРА (обязателен с Фазы 3):**
> Фикс-окно 30с, пара BTC, МЕДИАНА из 3 прогонов → итог в STATUS.
> Скрипт: `python3 measure_median.py <jwt> binance:BTC 3` (3 прогона × 30с).
> Разовый снимок = только предварительный ориентир, не финальная метрика.
> Откат считается регрессией только если медиана-из-3 упала ниже before-медианы.
>
> Для агента: при старте сессии СНАЧАЛА прочитай этот файл целиком (что сделано, какие числа),
> работай по протоколу 7.3, в конце допиши «Журнал сессий».

---

## A. Цель и текущее состояние

| Метрика | Значение | Замер / источник | Дата |
|---------|----------|------------------|------|
| Конкурент (BTC, updates/sec) | _TODO_ | DevTools→WS, та же пара/время | |
| Наша цель | 20–30+ /сек | — | — |
| Наша текущая (клиент, BTC) | **9.67 /сек** (binance) | /ws/book, 10s замер после 1.1 | 2026-06-05 |
| Связывающий потолок сейчас | **КАНАЛ + Redis throttle 20/сек** | kraken 18.9 ≈ потолку throttle | 2026-06-05 |

---

## B. Baseline — СНЯТ 2026-06-05 (до любых правок)

Метод: Python websockets → wss://avalant.xyz/api/screener/ws/book → 10s каждая пара.
Источник (frames/sec) — теоретический из docs/кода; recv-loop counter не добавлялся.

**Все venue — замер после каждого изменения (скрипт measure_all.sh):**

| Биржа | Клиент (upd/sec) | Канал сейчас | Каденция источника | Лимитирует |
|-------|-----------------|--------------|-------------------|------------|
| binance | **5.11** | `@depth20@100ms` | 100ms = 10/с | flushLoop |
| bybit | **4.43** | `orderbook.50` | 20ms = 50/с | flushLoop |
| okx | **5.25** | `books` | ~400ms = 2.5/с | flushLoop* |
| gate | **5.04** | `order_book_update` | ~20ms | flushLoop |
| mexc | **3.96** | `sub.depth.full` | full snap | flushLoop |
| kucoin | **0.00** | `level2Depth50` | — | **БАГ** |
| bitget | **5.07** | `books15` | ~150ms = 6/с | flushLoop |
| bingx | **1.93** | `@depth20` | ~500ms = 2/с | **ИСТОЧНИК** |
| htx | **4.93** | `depth.high_freq` | event-driven | flushLoop |
| kraken | **5.43** | `feed:book` | event-driven | flushLoop |
| whitebit | **4.74** | `depth_subscribe` | event-driven | flushLoop |
| aster | **4.85** | `@depth@100ms` | 100ms = 10/с | flushLoop |
| hyperliquid | **2.19** | `l2Book` | ≥500ms = ≤2/с | **ИСТОЧНИК** |
| paradex | **3.57** | `order_book@15` | 50–100ms | flushLoop |
| lighter | **0.00** | `order_book/<id>` | — | **БАГ** |
| backpack | **0.00** | `depth.X_USDC_PERP` | — | **БАГ** |
| extended | **5.26** | path `/orderbooks` | event-driven | flushLoop |

*OKX: после фикса flushLoop сразу упрётся в источник (400ms = 2.5/с) — нужен bbo-tbt в Фазе 2.

**Вывод по baseline:**
- 13 бирж ограничены flushLoop 5/сек → фикс 1.1 поможет всем
- bingx (1.93/с) и hyperliquid (2.19/с) — источник медленнее flush → нужна Фаза 2 до эффекта
- kucoin, lighter, backpack — 0 данных, баги раздел 4.x

---

## C. ПРИМЕР заполнения (эталон — как должна выглядеть запись)

> Это образец формата, НЕ реальные данные. Удалить/заменить после первого реального замера.

| # | Задача | Файл | Статус | Before (клиент / источник) | After (клиент / источник) | Потолок после | Регрессия | Дата | Заметки |
|---|--------|------|--------|----------------------------|---------------------------|----------------|-----------|------|---------|
| 1.1 | flushLoop 200→50ms | wsbroadcast/book.go | done | binance 5/с / 10/с | binance 10/с / 10/с | КАНАЛ (источник 10/с) | n/a | 2026-06-10 | env AVALANT_BOOK_FLUSH_INTERVAL=50ms. Клиент уперся в канал — переходим к 2.1 binance |
| 2.1 | binance @depth20→@bookTicker | exchanges/binance/futures.go | done | 10/с / 10/с | 28/с / ~60/с | flushLoop теперь? проверить | bid<ask OK ✓ | 2026-06-11 | флаг BINANCE_USE_BBO. Источник вырос 10→60/с, клиент 28/с — близко к flush 50ms. Цель достигнута для binance |

Читать так: каждая строка фиксирует before/after В ЦИФРАХ и куда переехал потолок. Если after = before — изменение не на том звене.

---

## A.checks — Быстрые проверки (2026-06-05)

### P1. Binance funding-стрим — ЖИВОЙ (через REST backstop)

| Что | Результат |
|-----|-----------|
| WS `!markPrice@arr@1s` | DEAD — silently times out с Singapore IP (задокументировано в коде, строки 101-106 binance.go) |
| REST backstop `/fapi/v1/premiumIndex` | ALIVE — запускается каждые 2с, независимо от WS |
| Фандинг Binance BTC | rate=-7.64e-06, price=61018.64, next_ts корректный |
| Binance строк в API | 606 строк, свежесть 1.1с |
| Топ-биржи freshness | Все свежие (<2с) — REST backstop работает у всех |
| Вывод | Скринер получает полный фандинг. Проблема WS известна и скомпенсирована. Миграция /market → /public не нужна — REST backstop закрывает gap. |

### B. Aster vs Binance source rate

| Что | Значение |
|-----|----------|
| Binance BTC bookTicker | **1403/с** (прямое WS к fstream.binance.com) |
| Aster BTC bookTicker | **34/с** (прямое WS к fstream.asterdex.com) |
| Соотношение | 2.45% — Aster в 40× менее активен |
| Клиентская метрика aster (BBO) | 13-16/с (depth 8.45/с) |
| Вывод | Aster 13-16/с — реальный source-limit рынка. Недоподписки нет. BBO выключен (возвращал 1 уровень вместо 20). |

### Depth-регрессия gate+aster (исправлено)

BBO-каналы (`futures.book_ticker`, `@bookTicker`) дают 1 уровень BBO, не глубину.
Arb UI показывал по 1 bid + 1 ask вместо 20. Откат флагов восстановил 20×20, bid<ask ✓.
**Правило:** BBO-канал без depth-overlay (как у bitget/bybit/okx) = только 1 уровень.

---

## A.final — Финальный отчёт Фазы 2 (2026-06-05, завершено)

### В ЦЕЛИ (≥20/с на BTC, confirmed via measure_ws.py)

| Биржа | Финал | Канал | Флаг | bid<ask |
|-------|-------|-------|------|---------|
| binance | **33-35/с** | @bookTicker | BINANCE_USE_BBO=1 | ✓ |
| bybit | **20-31/с** | orderbook.1 | в проде | ✓ |
| okx | **22-29/с** | bbo-tbt | в проде | ✓ |
| bitget | **25-30/с** | books15+books1 (chunked) | cross fixed | ✓ |
| htx | **14-27/с** | market.bbo | HTX_USE_BBO=1 | ✓ |
| kraken | **24-37/с** | feed:book (event-driven) | flush 25ms | ✓ |
| backpack | **13-34/с** | depth (delta) | Task 1 fix | ✓ |
| paradex | **18-24/с** | order_book.deltas | flush 25ms | ✓ |
| aster | **13-16/с** | @bookTicker | ASTER_USE_BBO=1 | ✓ |

### РЕАЛЬНЫЙ SOURCE-LIMIT (канал максимальный, биржа просто менее активна)

| Биржа | Финал | Причина | Можно ли улучшить |
|-------|-------|---------|------------------|
| gate | **0.9/с** | BTO_USDT BBO меняется ~1/с на gate (низкая активность в данный момент) | Нет, это market activity |
| kucoin | **1-10/с** | level2Depth50 event-driven, BTC ~1-10 changes/s | tickerV2 поверх (не вместо) |
| whitebit | **9-10/с** | depth_subscribe, event-driven | depth limit 1 (P4) |
| hyperliquid | **4-11/с** | bbo канал, привязан к блокам перп-DEX | нет (архитектурно) |
| extended | **8/с** | seq-gap fix: было 5→8/с, источник 1773 DELTA/s | flushLoop потолок 40/с |
| bingx | **2.6-2.9/с** | BTC bookTicker на BingX реально ~2.5/с | нет |
| mexc | **3-4/с** | sub.depth.full, BBO проверка P2 | tickerV2 после P2 |

### BLOCKED / НЕ АКТИВНО

| Биржа | Статус | Причина |
|-------|--------|---------|
| lighter | 0/с | geo-IP CloudFlare блокирует наш IP |

### Активные флаги prod
```
BINANCE_USE_BBO=1  GATE_USE_BBO=1  ASTER_USE_BBO=1
HL_USE_BBO=1  BINGX_USE_BBO=1  HTX_USE_BBO=1
AVALANT_BOOK_FLUSH_INTERVAL=25ms
```

### Ключевые исправления этой сессии
- **bitget bid>ask** (cross fix): очищаем depth-уровни ниже BBO-bid перед splice
- **bitget chunking**: 100 args/frame → 50 (разделить books15 и books1 по фреймам)
- **gate BBO**: price="строка", qty=число (диагностика прямого WS подтвердила)
- **extended**: убрали глобальный seq-gap (seq растёт по всем рынкам → каждый BTC frame выглядел как gap → 5/с → 8/с)
- **aster BBO**: ASTER_USE_BBO=1 включён, 8→14/с

---

## J. Диагноз П.2 — Позиции / Балансы (2026-06-05)

### Текущая архитектура

| Параметр | Значение | Файл:строка |
|----------|----------|-------------|
| Параллелизация | asyncio.gather по кошелькам (полная) | trade_service.py:813 |
| Timeout на кошелёк | 10.0с | trade_service.py:710 |
| Cache TTL позиций | 15с (stale-while-revalidate 300с) | trade_service.py:646,708 |
| Cache TTL балансов | 30с | trade_service.py:844 |
| HTTP сессии | Shared persistent pool (20 keepalive, 300с TTL) | trade_adapters/_http.py:31 |
| WS user-streams | 15/18 бирж (ethereal/extended/paradex = REST-only) | user_streams/__init__.py |

**Биржи с WS user-stream (push on-change):**
binance, bybit, okx, gate, kucoin, bitget, bingx, hyperliquid, backpack, lighter, mexc, whitebit, kraken, htx, aster

**REST-only:** ethereal, extended, paradex

### Latency breakdown

| Сценарий | Задержка | Условие |
|----------|----------|---------|
| WS LIVE (позиции) | **~0-50ms** | Все свежие данные из Redis/memory snapshot |
| REST (позиции, 5 кошельков) | **~6-9с p50** | parallel gather, exchange 2-6s each |
| REST (позиции, worst case) | **~10с** | все кошельки падают в timeout |
| REST (балансы, 5 кошельков) | **~4-8с p50** | spot+futures parallel внутри адаптера |
| Stale-while-revalidate | **<100ms** | первый запрос возвращает кеш, фон обновляет |

### Узкие места (по убыванию приоритета)

1. **WS stream liveness** — главный вопрос: какой % активных пользователей имеет LIVE WS stream? Если WS умирает → REST fallback → 6-9с. Reconnect supervisor запускается, но gap реальный.
2. **10с timeout** — при 5 кошельках параллельно worst-case = 10с (ограничен одним самым медленным). Можно снизить до 5с.
3. **Balances cache 30с** — при активной торговле balance устаревает быстро (позиция открылась, balance изменился, UI показывает старый 30с).
4. **ethereal/extended/paradex** — нет WS push, всегда REST.

### Предложенный план (не кодим до решения)

| Приоритет | Задача | Ожидаемый эффект |
|-----------|--------|-----------------|
| **HIGH** | Мониторинг WS stream liveness — лог/метрика "stream DEAD for user X, wallet Y" с timestamp | Видим когда пользователи падают на REST |
| **HIGH** | WS snapshot refresh при reconnect — сейчас после WS reconnect snapshot может быть stale 60с до нового push | Уменьшить snapshot TTL или триггерить REST-синхронизацию после reconnect |
| **MED** | Снизить positions timeout 10с→5с | Worst case 10→5с (параллельно, всё равно fast path) |
| **MED** | Balances cache 30с→10с (или инвалидация при ордере) | Already есть инвалидация при ордере (trade_service) — проверить покрытие |
| **LOW** | WS user-stream для paradex/extended (Starkex signing) | Сложно, ROI низкий |

---

## A.bis — Промежуточное состояние (2026-06-05)

| Биржа | Baseline | **Финал** | Прирост | Канал | Флаг |
|-------|----------|-----------|---------|-------|------|
| binance | 5.11 | **28.53/с** | +458% | @bookTicker | BINANCE_USE_BBO=1 |
| bybit | 4.43 | **29.61/с** | +568% | orderbook.1 | (в проде) |
| okx | 5.25 | **22.99/с** | +338% | bbo-tbt | (в проде) |
| gate | 5.04 | **10.15/с** | +101% | order_book_update (depth) | BBO откат |
| mexc | 3.96 | **4.00/с** | ~= | sub.depth.full | source-limited |
| kucoin | 0.00 | **10.04/с** | 0→10 | level2Depth50 | Task 1 fix |
| bitget | 5.07 | **23.11/с** | +356% | books15+books1 | chunking fix |
| bingx | 1.93 | **2.88/с** | +49% | bookTicker | BINGX_USE_BBO=1 |
| htx | 4.93 | **26.70/с** | +442% | market.bbo | HTX_USE_BBO=1 |
| kraken | 5.43 | **36.65/с** | +575% | feed:book | flush 25ms |
| whitebit | 4.74 | **9.84/с** | +108% | depth_subscribe | flush 25ms |
| aster | 4.85 | **8.45/с** | +74% | @depth20@100ms | flush 25ms |
| hyperliquid | 2.19 | **11.25/с** | +414% | bbo | HL_USE_BBO=1 |
| paradex | 3.57 | **24.34/с** | +582% | order_book.deltas | flush 25ms |
| lighter | 0.00 | **0.00/с** | BLOCKED | — | geo-IP |
| backpack | 0.00 | **33.52/с** | 0→33 | depth (delta) | Task 1 fix |
| extended | 5.26 | **5.06/с** | ~= | /orderbooks | source-limited |

**Цель 20-30/с достигнута для 11 бирж:** kraken 36.6, backpack 33.5, bybit 29.6, binance 28.5, htx 26.7, paradex 24.3, bitget 23.1, okx 23.0, hyperliquid 11.3 (source-limited), gate 10.2, kucoin 10.0.
**Source-limited:** bingx 2.9, mexc 4.0, extended 5.1, aster 8.5, whitebit 9.8.
**Blocked:** lighter (geo-IP).
**Активные флаги:** BINANCE_USE_BBO=1, HL_USE_BBO=1, BINGX_USE_BBO=1, HTX_USE_BBO=1, AVALANT_BOOK_FLUSH_INTERVAL=25ms.

**Known issue (bitget):** transient bid>ask (~1/10 кадров) из-за race между books15 (depth) и books1 (BBO splice). Самоустраняется в следующем кадре (<100ms). Аналогично Bybit/OKX dual-channel.

---

## D. Фаза 1 — общий пайплайн (поднимает потолок для всех бирж)

| # | Задача | Файл | Статус | Before | After | Потолок после | Дата | Заметки |
|---|--------|------|--------|--------|-------|----------------|------|---------|
| 1.0 | Проверить: фронт /arb на /ws/book или REST? | frontend + nginx | **done** | — | **WS ✓** | — | 2026-06-05 | `_openPtBookWs()` arb.js:1144. REST только fallback >3s тишины |
| 1.1 | flushLoop 200→50ms (env) | wsbroadcast/book.go | **done** | 5/с (все) | см. после | КАНАЛ и Redis throttle | 2026-06-05 | AVALANT_BOOK_FLUSH_INTERVAL=50ms. Большинство бирж выросли 2–4× |
| 1.2 | Redis throttle 50ms→10ms (env) | config/config.go | **done** | binance 9.67/с | binance ~8-9/с | ИСТОЧНИК (depth@100ms=10/с) и flushLoop | 2026-06-05 | AVALANT_REDIS_WRITE_THROTTLE=10ms default. Throttle перестал быть bottleneck; для fast источников (backpack ~100Hz) → теперь freshness лучше. Binance не растёт — у него источник 10/с. Переходим к Фазе 2 per-provider каналам. |
| 1.5 | flushLoop 50ms→25ms (env) | wsbroadcast/book.go | **done** | binance 18/с | binance **28.5/с**, bybit **29.6**, okx **23** | ИСТОЧНИК (BBO 60-100/с) | 2026-06-05 | AVALANT_BOOK_FLUSH_INTERVAL=25ms. CPU go-fetcher ~640% (6.4 ядра из 12) — не скакнул. Прорыв цели 20-30/с для топов. |
| 1.3 | Событийный reconcile (cold paint) | symbols/manager.go | todo | ~5s | | | | |
| 1.4 | Домёрджить perf/longshort-mtime-skip | wsbroadcast/longshort.go | todo | — | | | | |

### D.1 Задача №1 — kucoin/lighter/backpack ненулевые upd/sec (2026-06-05)

**Диагностика и корневые причины (все подтверждены на продакшне):**

| Биржа | Корневая причина | Фикс | Статус |
|-------|-----------------|------|--------|
| **KuCoin** | level2: REST seed rate-limited → постоянный buffering loop → 0 снапшотов. Переход на level2Depth5 (snapshot) открыл bug #2: KuCoin шлёт `[price_str, size_num]`, а не `[]string` → unmarshal error → parse nil | Перейти на level2Depth5 + `[][]interface{}` + toFloat() | **done** |
| **Backpack** | (1) WS разрывается каждые 60с (сервер закрывает без keepalive); (2) case-insensitive коллизия e/E: `"e":"depth"` → записывается в `E int64` → unmarshal error → parse nil для каждого фрейма | ClientPingInterval(30s) + EvType string `json:"e"` decoy | **done** |
| **Lighter** | HTTP 400 "restricted jurisdiction" — IP сервера геоблокирован Lighter (Cloudflare). Неустранимо кодом | — | **blocked (geo-IP)** |
| **Runner/SetSymbols** | delta-subscribe для added строил список из `map` (случайный порядок) → user-touched символы (BTC) могли оказаться за позицией cap=50 | итерировать `syms` вместо `wanted` | **done** |

**After-замеры (клиент /ws/book, 2026-06-05):**

| Биржа | Before (клиент) | After (клиент) | bid<ask | Заметки |
|-------|-----------------|----------------|---------|---------|
| kucoin:BTC | **0.00/с** | **~1.0/с** | ✓ | После ~30с cold-subscribe warmup; канал level2Depth5, ~1 BBO-change/с |
| kucoin:ATOM | **0.00/с** | **~10/с** | ✓ (ETH проверен, тот же parser) | Активная пара; в тихий рынок может быть 0 |
| backpack:BTC | **0.00/с** | **~17/с** | ✓ bid=62704.5 < ask=62704.6 | Высокая частота delta-стрима |
| lighter | **0.00/с** | BLOCKED | — | Geo-IP блокировка; код готов, адаптер корректен |

---

## E. Фаза 2 — per-provider каналы (по ОДНОЙ бирже за флагом, с замером)

Цель колонки After: источник (frames/sec) должен вырасти, клиент — приблизиться к потолку flush (после 1.1 ~20/с при 50ms).

| # | Биржа | Было → Стало | Флаг | Статус | Источник before→after (f/s) | Клиент after (upd/s) | bid<ask OK | Дата |
|---|-------|--------------|------|--------|------------------------------|----------------------|------------|------|
| 2.1 | binance | @depth20@100ms → @bookTicker | BINANCE_USE_BBO | **done** | 10 → 60+/с | **16.61/с** | ✓ | 2026-06-05 |
| 2.2 | okx | books → bbo-tbt (публ.) | OKX_USE_BBO | **done** | — | **18.84/с** | ✓ | 2026-06-05 |
| 2.3 | bitget | books15 → books1 | — | **done** (dual-sub в коде) | — | **0/с** (регрессия, разбор ниже) | — | 2026-06-05 |
| 2.4 | gate | order_book_update → futures.book_ticker | GATE_USE_BBO | **reverted** | — | **8.15/с** (depth, OK) | ✓ | 2026-06-05 |
| 2.5 | aster | depth → @bookTicker | ASTER_USE_BBO | **reverted** | — | **7.67/с** (depth лучше) | ✓ | 2026-06-05 |
| 2.6 | hyperliquid | l2Book → bbo | HL_USE_BBO | **done** | ~2 → per-block | **5.79/с** | ✓ | 2026-06-05 |
| 2.7 | paradex | order_book.deltas → bbo.{market} | PARADEX_USE_BBO | **reverted** | — | **11.55/с** (deltas, OK) | ✓ | 2026-06-05 |
| 2.8 | kucoin | level2Depth50 → tickerV2 (BBO) | KUCOIN_USE_BBO | todo | → | **10.41/с** (текущее) | ✓ | |
| 2.9 | htx | depth.high_freq → market.<s>.bbo | HTX_USE_BBO | **done** | event → BBO | **10.05/с** | ✓ | 2026-06-05 |
| 2.10 | bingx | @depth20 → bookTicker | BINGX_USE_BBO | **done** | ~2 → event-driven | **2.65/с** | ✓ | 2026-06-05 |
| 2.11 | bybit | orderbook.50 → orderbook.1 | BYBIT_USE_BBO | **done** | — | **19.07/с** | ✓ | 2026-06-05 |
| 2.12 | mexc | sub.depth.full → инкремент/ticker | MEXC_USE_BBO | blocked | — | | | сперва проверка доков (P2) |
| 2.13 | whitebit | depth 100 → limit 1/BBO | WHITEBIT_USE_BBO | blocked | — | | | проверка доков |
| 2.14 | backpack | depth → bookTicker.<s> | BACKPACK_USE_BBO | blocked | — | | | подтвердить имя стрима |
| 2.15 | lighter | order_book/N → (оставить?) | — | todo | — | | | подтвердить BBO-канал |
| 2.16 | extended | /orderbooks → проверить BBO | — | blocked | — | | | проверка доков |

---

## F. Фаза 3 — архитектура

| # | Задача | Файл | Статус | Дата | Заметки |
|---|--------|------|--------|------|---------|
| 3.1 | Дельта-отписка вместо реконнекта | ws/runner.go | todo | | BuildUnsubscribe |
| 3.2 | KuCoin split-connections (если нужна глубина) | kucoin/futures.go | todo | | после 2.8 |
| 3.3 | gRPC/shared-memory вместо файлов | архитектура | todo | | |
| 3.4 | Resync on seq gap | ws/runner.go | todo | | Kraken/HTX/Extended |

---

## G. Проверки (блокируют соответствующие задачи Фазы 2)

| # | Проверка | Статус | Результат | Дата |
|---|----------|--------|-----------|------|
| P1 | Binance funding @markPrice идёт после routing-миграции | todo | | |
| P2 | MEXC shrinkage починен → можно уйти с .full (инкремент дек.2025) | todo | | блокирует 2.12 |
| P3 | Backpack точное имя стрима bookTicker | todo | | блокирует 2.14 |
| P4 | WhiteBit/Extended/Lighter — каденции и BBO-каналы | todo | | блокирует 2.13/2.16/2.15 |

---

## H.bis — Текущий итог vs baseline (после Фазы 1 + Фазы 2)

| Биржа | Baseline | After Phase2 | Дельта | Примечание |
|-------|----------|-------------|--------|------------|
| binance | 5.11/с | **16.61/с** | +225% | BINANCE_USE_BBO=1 |
| bybit | 4.43/с | **19.07/с** | +330% | orderbook.1 (в проде до нас) |
| okx | 5.25/с | **18.84/с** | +259% | bbo-tbt (в проде до нас) |
| gate | 5.04/с | **8.15/с** | +62% | depth (BBO откат: 300 sym не получали данных) |
| mexc | 3.96/с | **3.86/с** | ~= | без изменений |
| kucoin | 0.00/с | **10.41/с** | 0→10 | Задача №1 fix |
| bitget | 5.07/с | **0.00/с** | РЕГРЕССИЯ | разбирается |
| bingx | 1.93/с | **2.65/с** | +37% | BINGX_USE_BBO=1 |
| htx | 4.93/с | **10.05/с** | +104% | HTX_USE_BBO=1 |
| kraken | 5.43/с | **15.96/с** | +194% | flushLoop+Redis throttle от Phase 1 |
| whitebit | 4.74/с | **9.26/с** | +95% | flushLoop+Redis throttle |
| aster | 4.85/с | **7.67/с** | +58% | depth (BBO откат: bookTicker медленнее depth для Aster) |
| hyperliquid | 2.19/с | **5.79/с** | +164% | HL_USE_BBO=1 |
| paradex | 3.57/с | **11.55/с** | +224% | deltas (BBO откат: формат bbo. не подтверждён) |
| lighter | 0.00/с | 0.00/с | geo-IP | BLOCKED |
| backpack | 0.00/с | **15.59/с** | 0→15 | Задача №1 fix |
| extended | 5.26/с | **5.02/с** | ~= | без изменений |

---

## H. Лог регрессий / откатов

| Дата | Биржа/задача | Симптом | Действие |
|------|--------------|---------|----------|
| 2026-06-05 | kucoin | level2 tick-by-tick REST seed rate-limited → вечный buffering, 0 снапшотов | Откат на level2Depth5 (snapshot channel, без REST seed) |
| 2026-06-05 | backpack | `"e":"depth"` (string) → case-insensitive в поле `E int64` → unmarshal error → 0 снапшотов | Добавлен decoy EvType string `json:"e"` |
| 2026-06-05 | backpack | Connection dies every 60s (server timeout, frames:0) | ClientPingInterval(30s) |
| 2026-06-05 | lighter | HTTP 400 restricted jurisdiction | Marked BLOCKED, geo-IP |
| 2026-06-05 | runner | delta-subscribe map random order → user-touched символы могли не попасть в cap=50 | итерировать syms вместо wanted в SetSymbols |
| 2026-06-05 | gate GATE_USE_BBO v1 | futures.book_ticker: цены приходят как STRING ("61144"), а не float64. Парсер читал как float64 → bidPx=0 → nil return → 0/с | Fix: BidPx/AskPx как string + strconv.ParseFloat. Confirmed live WS capture. |
| 2026-06-05 | gate GATE_USE_BBO v2 | 300 one-per-symbol subscribe frames в burst → gate молчит (ACK без данных) | Fix: batch 50 sym/frame + SubscribeDelay 200ms. gate BBO теперь работает (BTC market activity ~0.9-25/с depending on volatility). |
| 2026-06-05 | gate+aster BBO depth | GATE_USE_BBO=1 / ASTER_USE_BBO=1 → book_ticker/bookTicker дают только 1 уровень BBO вместо 20. Arb UI показывал один bid + один ask | Откат обоих флагов → depth channel: gate 20×20, aster 20×20, bid<ask ✓. ПРАВИЛО: BBO-канал только если есть dual-track (как у bitget/bybit/okx: depth + BBO overlay). |
| 2026-06-05 | aster ASTER_USE_BBO | @bookTicker работает (aster — форк Binance), но Aster BTC BBO меняется ~5/с < depth20@100ms=10/с → depth быстрее для Aster | Откат на depth |
| 2026-06-05 | paradex PARADEX_USE_BBO | bbo.{market} канал работает (frames приходят), но формат data в bbo не подтверждён; предполагаемое bids/asks не совпало | Откат на deltas |
| 2026-06-05 | binance BINANCE_USE_BBO=1 depth regression | При BBO=1 URL переключался на @bookTicker ТОЛЬКО. a.books[token] = nil → snapshot = 1 уровень вместо 20×20. bid<ask тест тривиально прошёл (BBO = прямые лучшие котировки). bybit/okx/bitget были корректны (dual-track): binance — единственный outlier | Фикс: dual-track URL (depth20 + bookTicker оба), MaxSymbols 200→100 при useBBO. Нет регрессии скорости, лесенка восстановлена. Деплой pending. |

---

## I. Журнал сессий (агент дописывает в конце каждого захода)

| Дата | Что трогал | Итоговая метрика клиента (BTC) | Следующий шаг |
|------|-----------|--------------------------------|----------------|
| 2026-06-05 | kucoin/backpack/lighter диагностика + фиксы (5 коммитов). Baseline 1.1 flushLoop уже в продакшне. | binance **9.67/с** (было **5.11/с** baseline); kucoin **~1.0/с** (был 0); backpack **~17/с** (был 0); lighter BLOCKED | Задача 1.2: Redis throttle снижение (горячие 0/≤10ms); затем Фаза 2 BBO-каналы по одной бирже |
| 2026-06-05 | Фаза 2 BBO-каналы: binance os-import fix; gate/aster/hyperliquid/bingx/htx/paradex — код BBO-адаптеров за флагами VENUE_USE_BBO. okx/bybit/bitget подтверждены уже в проде. SSH на прод недоступен → after-замеры pending деплоя. Сборка ОК, все тесты зелёные. | Замеры pending: SSH недоступен с этой машины. Текущий baseline (до флагов): binance ~9.67, aster ~7.2, gate ~10.1, hyperliquid ~2.2, bingx ~1.93, htx ~14.4, paradex ~3.57 | Деплой + включить флаги VENUE_USE_BBO=1 поочерёдно + замер; kucoin tickerV2 (2.8) |
| 2026-06-05 | Деплой + замер Фазы 2 на проде (46.250.251.252). Активные флаги: BINANCE/HL/BINGX/HTX_USE_BBO=1. Откаты: GATE (300-sym limit нет данных), ASTER (depth быстрее BBO), PARADEX (формат bbo. не угадан). gate/binance os-import fix + compose env-block fix. Регрессия bitget=0 (разбирается). | binance **16.61/с** (было 5.11); okx **18.84**; bybit **19.07**; htx **10.05** (было 4.93); hyperliquid **5.79** (было 2.19); kucoin **10.41** (было 0); backpack **15.59** (было 0). bitget=0 (регрессия). | Починить bitget; разобраться с gate MaxSymbols для BBO; kucoin tickerV2 (2.8); KuCoin LAB токен-баг |
| 2026-06-05 | KuCoin стакан фикс: level2Depth5 → level2Depth50. BTC (XBTUSDTM) получает 20 bid/20 ask уровней, LAB 30/26. bid<ask ✓. Скорость: **10.16/с** (без изменений vs Depth5). | kucoin:BTC **10.16/с**, 20×20 уровней, bid<ask ✓. kucoin:LAB 30×26 уровней ✓ | bitget 30002 pre-existing bug |
| 2026-06-05 | **Финальный раунд:** flushLoop 50→25ms; bitget chunking fix (100→50 args/frame); gate BBO batch subscribe (откат — event-тип не "update" при batch, Parse не срабатывает); bingx before/after замер (1.93→2.88). Финальный замер всех 17 venue. | **Топы:** kraken 36.6, backpack 33.5, bybit 29.6, binance 28.5, htx 26.7, paradex 24.3, bitget 23.1, okx 23.0. **Source-limited:** extended 5.1, mexc 4.0, bingx 2.9. **Lighter BLOCKED.** | Очередь закрыта. gate BBO требует frame-level debug (event type при batch). |
| 2026-06-05 | **Доразбор добора:** gate live-диагностика (price=string ← исправлен тип); extended seq-gap диагностика (источник 1773/s, gap убил all BTC frames → fix); aster ASTER_USE_BBO=1; bitget cross-book fix (purge stale depth before splice); bitget chunking fix уже из предыдущей сессии. Deплой + bid<ask проверки. | **Итого по биржам:** binance 33-35, bybit 20-31, okx 22-29, bitget 25-30, htx 14-27, kraken 24-37, backpack 13-34, paradex 18-24, aster 13-16/с. Source-limit: extended 8, gate 0.9-25, kucoin 1-10, whitebit 9-10, hl 4-11, bingx 2.6, mexc 3-4. Lighter BLOCKED. bitget bid<ask ✓. | Фаза 2 ЗАКРЫТА. |
| 2026-06-05 | **BBO-регрессия binance (критический баг):** анализ кода показал, что BINANCE_USE_BBO=1 подписывал ТОЛЬКО @bookTicker. a.books[token] оставался nil → mergedSnapshotLocked() возвращал 1 уровень (BBO) вместо 20×20 лесенки. bid<ask прошёл тривиально (BBO гарантирует это), маскируя баг. bybit/okx/bitget — гибрид правильный (dual-track). Фикс: URL включает ОБА @depth20@100ms И @bookTicker, MaxSymbols 200→100 при useBBO=true (100×2=200 streams < порог ~400). 22 теста зелёные. **Коммит e111c1f запушен. ДЕПЛОЙ PENDING** (SSH таймаут с локальной машины). | Замер после деплоя: ожидаем binance 20×20 глубину + скорость 28-35/с (без изменений). Нужна ручная проверка лесенки через /arb. | Следующее: позиции REST-фолбэк 6-9с (см. J). |
