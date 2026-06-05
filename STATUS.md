# Avalant — Orderbook Optimization · STATUS (живой журнал)

> Рабочий журнал прогресса по `ORDERBOOK_OPTIMIZATION.md`. Обновлять после КАЖДОГО изменения.
> **Правило: задача не done без after-замера. «Сделал» без числа = не сделано.**
> Метрика — updates/sec на клиенте (цель 20–30+). Как мерить — Раздел 7.2 основного дока.
> Дата формата YYYY-MM-DD. Статусы: `todo` / `in-progress` / `done` / `blocked` / `reverted`.
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

## D. Фаза 1 — общий пайплайн (поднимает потолок для всех бирж)

| # | Задача | Файл | Статус | Before | After | Потолок после | Дата | Заметки |
|---|--------|------|--------|--------|-------|----------------|------|---------|
| 1.0 | Проверить: фронт /arb на /ws/book или REST? | frontend + nginx | **done** | — | **WS ✓** | — | 2026-06-05 | `_openPtBookWs()` arb.js:1144. REST только fallback >3s тишины |
| 1.1 | flushLoop 200→50ms (env) | wsbroadcast/book.go | **done** | 5/с (все) | см. после | КАНАЛ и Redis throttle | 2026-06-05 | AVALANT_BOOK_FLUSH_INTERVAL=50ms. Большинство бирж выросли 2–4× |
| 1.2 | Redis throttle 50→≤33ms/байпас (env) | redisbus/writer.go | todo | 20/с | | | | после замера 1.1 |
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
| 2.1 | binance | @depth20@100ms → @bookTicker | BINANCE_USE_BBO | todo | 10 → | | | |
| 2.2 | okx | books → bbo-tbt (публ.) | OKX_USE_BBO | todo | 10 → | | | |
| 2.3 | bitget | books15 → books1 | BITGET_USE_BBO | todo | ~7 → | | | |
| 2.4 | gate | order_book_update → futures.book_ticker | GATE_USE_BBO | todo | → | | | |
| 2.5 | aster | depth → @bookTicker | ASTER_USE_BBO | todo | 10 → | | | |
| 2.6 | hyperliquid | l2Book → bbo | HL_USE_BBO | todo | ~2 → | | | |
| 2.7 | paradex | order_book@15 → bbo.{market} | PARADEX_USE_BBO | todo | → | | | |
| 2.8 | kucoin | level2Depth50 → tickerV2 (BBO) | KUCOIN_USE_BBO | todo | → | | | + снимает 50-cap |
| 2.9 | htx | depth.high_freq → market.<s>.bbo | HTX_USE_BBO | todo | → | | | |
| 2.10 | bingx | @depth20 → bookTicker | BINGX_USE_BBO | todo | 10 → | | | gzip+text ping |
| 2.11 | bybit | orderbook.50 → orderbook.1 | BYBIT_USE_BBO | todo | 50 → | | | опц., уже 20ms |
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

## H. Лог регрессий / откатов

| Дата | Биржа/задача | Симптом | Действие |
|------|--------------|---------|----------|
| 2026-06-05 | kucoin | level2 tick-by-tick REST seed rate-limited → вечный buffering, 0 снапшотов | Откат на level2Depth5 (snapshot channel, без REST seed) |
| 2026-06-05 | backpack | `"e":"depth"` (string) → case-insensitive в поле `E int64` → unmarshal error → 0 снапшотов | Добавлен decoy EvType string `json:"e"` |
| 2026-06-05 | backpack | Connection dies every 60s (server timeout, frames:0) | ClientPingInterval(30s) |
| 2026-06-05 | lighter | HTTP 400 restricted jurisdiction | Marked BLOCKED, geo-IP |
| 2026-06-05 | runner | delta-subscribe map random order → user-touched символы могли не попасть в cap=50 | итерировать syms вместо wanted в SetSymbols |

---

## I. Журнал сессий (агент дописывает в конце каждого захода)

| Дата | Что трогал | Итоговая метрика клиента (BTC) | Следующий шаг |
|------|-----------|--------------------------------|----------------|
| 2026-06-05 | kucoin/backpack/lighter диагностика + фиксы (5 коммитов). Baseline 1.1 flushLoop уже в продакшне. | binance **9.67/с** (было **5.11/с** baseline); kucoin **~1.0/с** (был 0); backpack **~17/с** (был 0); lighter BLOCKED | Задача 1.2: Redis throttle снижение (горячие 0/≤10ms); затем Фаза 2 BBO-каналы по одной бирже |
