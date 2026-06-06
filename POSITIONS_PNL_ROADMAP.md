# Avalant — Позиции / PNL / Арб-пары / Закрытие — Роадмап (А→Я)

> План работ по странице `/arb`: отслеживание позиций, расчёт PNL, детект арб-пар,
> закрытие (perp+spot), вкладка PNL закрытых позиций. От фундамента до вишенок.
> Принцип: ДОСТОВЕРНОСТЬ → ФИЧИ. Нельзя строить PNL-вкладку поверх врущих чисел.
> Каждый шаг: что / зачем / как проверить / зависимость. Статусы вести в STATUS.md.

---

## 0. Контекст и железные правила

- Это **арб-продукт**. В дельта-нейтральной паре ценовой PNL двух ног гасится — реальная цифра
  это маленькая РАЗНИЦА. Поэтому рассинхрон ног и стейл-mark врут не косметически, а искажают
  главный показатель. **Точность здесь важнее скорости.**
- 18 venue. У 3 (paradex, ethereal, extended) НЕТ user-stream → всегда REST. Это влияет на план.
- Правила работы (как и в orderbook-проекте): одно изменение за раз, за флагом, замер до/после,
  регрессия проверяется, не нашёл решение → откат + честный статус. done только с проверкой.

---

## ПРЕДУСЛОВИЯ (блокеры — без них половину нельзя ни проверить, ни запустить)

| # | Блокер | Зачем | Зависит от |
|---|--------|-------|------------|
| P1 | nginx / TLS поднять (сейчас restarting) | Без HTTPS сайт недоступен — нельзя даже открыть UI | НЕ нужны креды, чинить сразу |
| P2 | TG-токен + создать user в БД | Логин не работает без этого | твои креды |
| P3 | Реальный кошелёк (API-ключи) + 1 маленькая позиция (или арб-пара) | **PNL/close НЕЛЬЗЯ проверить на пустой БД** — прошлый замер дал ложное "FAST 27ms" на 401 | твои ключи + $ |

**Критично:** живая верификация Phase 1+ требует P3. Без реальной позиции агент закоммитит
«зелёные тесты» поверх непроверенной логики — ровно как с binance BBO (тест проходил, прод сломан).
До P3 агент может делать только read-only диагностику (Phase 0) и рефактор за флагом без приёмки.

---

## PHASE 0 — Фундамент и диагностика (read-only, кредов не требует)

| # | Задача | Зачем | Как проверить |
|---|--------|-------|---------------|
| 0.1 | Матрица close_position по 18 провайдерам | Понять, где backend закрытия реально работает, где падает/не реализован, spot vs perp | Таблица: venue / perp-close готов / spot-close готов / механизм / статус |
| 0.2 | Матрица fetch позиций по venue (WS user-stream vs REST) | Знать, где свежесть push, где poll 5-10с (paradex/ethereal/extended всегда REST) | Таблица из карты + подтвердить актуальность |
| 0.3 | Дата-модель арб-пары | Сейчас сервер НЕ группирует ноги — фронт суммирует. Нужно серверное понятие «пара» | Спека: что делает 2 позиции парой (см. Phase 2) |
| 0.4 | Дата-модель закрытой позиции для PNL-вкладки | Нужна персистентность реализованного PNL после закрытия | Спека таблицы closed_positions / pnl_history |

Phase 0 — чистое чтение и проектирование. Можно делать без P2/P3.

### 0.3 — Дата-модель арб-пары (LIVE open + closed). Текущее состояние

**Уже в БД** (`backend/db/models.py:640`, table `trade_positions`, migration `d7e8f9a0b1c2`):

```python
class TradePosition:
    id, user_id, kind, pair_kind, status, symbol
    # leg A (long для long_short, spot для spot_short)
    leg_a_wallet_id, leg_a_exchange, leg_a_side, leg_a_qty,
    leg_a_entry_price, leg_a_exit_price,
    leg_a_realized_pnl_usd, leg_a_funding_pnl_usd, leg_a_fees_usd,
    leg_a_open_order_id, leg_a_close_order_id, leg_a_market
    # leg B (short)
    leg_b_*  # симметричные поля, nullable для kind=single
    source                # platform | reconcile | fills_backfill
    realized_pnl_usd      # агрегированный по обеим ногам
    entry_spread_pct, exit_spread_pct
    opened_externally, closed_externally
    opened_at, closed_at
    arb_position_id       # FK на arb_positions (intent-уровень)
```

**Поля для пары** (`kind = 'pair'`):
- `pair_kind`: `'long_short'` (перп/перп на разных биржах) | `'spot_short'` (спот LONG + перп SHORT) | NULL
- Обе ноги обязательны (leg_a + leg_b NOT NULL)
- `leg_a_market` ∈ {`'futures'`, `'spot'`} различает legs.

**Auto-pair правило** (`trade_service._pnl_can_pair`, line 1055-1087):
- Same symbol (uppercase нормализация).
- Notional diff% ≤ spread% + **12% толеранс** (было 5%, расширено для реальных арбов).
- Opened within **5-минутного окна**.
- Не помечена `'unpaired'` юзером.

**Manual decisions** (`TradePairDecision`, table `trade_pair_decisions`):
- `leg_a_key` / `leg_b_key` — стабильный fingerprint `{symbol}|{exchange}|{rounded_entry}` или `spot|{symbol}|{exchange}` для спота.
- `decision`: `'paired'` | `'unpaired'`.
- Unique `(user_id, leg_a_key, leg_b_key)` — отдельные ручные пары.

**Пробел для Phase 2/3** (НЕ менять сейчас):
- Открытая live пара на `/arb` сейчас вычисляется на фронте каждый рендер (`_ptPairPositions`). Сервер вьюхи `kind='pair', status='open'` строки **не пишет** — только при закрытии полной пары (`reconcile_service` или `fills_backfill_service`). Это сознательно (не дублировать live state), но Phase 2.1 потребует серверного эквивалента для алертов/триггеров.
- Сейчас нет «pair_intent» — намерения юзера. ArbPosition (`arb_position_id`) задумано как intent-обертка, но **не используется** на open path. В live UI пары существуют только как агрегация двух TradePosition single рядом друг с другом.

### 0.4 — Дата-модель закрытой позиции для PNL-вкладки. Текущее состояние

**Та же таблица** `trade_positions` со `status='closed'` обслуживает PNL-вкладку. Migration `d7e8f9a0b1c2` + дополнения в `k0p1q2r3s4t5` (`leg_*_market` + `source`).

**Источники закрытой записи** (3 sources):
- `'platform'` — мы поставили close-order через `/api/trade/close`. Пишется в `reconcile_service` или `trade_service.close_position` post-fill.
- `'reconcile'` — `reconcile_service` обнаружил исчезновение позиции из `list_positions` (закрылось вне UI — venue stop-loss, manual trade в их веб-морде).
- `'fills_backfill'` — `fills_backfill_service` восстановил историю из `/userTrades` за 7 дней. Реконструирует net=0 моменты.

**Поля для PNL-вкладки**:
- `realized_pnl_usd` — агрегированный по обеим ногам (для пары) или leg_a (для single).
- `leg_a_realized_pnl_usd` / `leg_b_realized_pnl_usd` — per-leg для отображения breakdown.
- `leg_a_funding_pnl_usd` / `leg_b_funding_pnl_usd` — accumulated funding.
- `leg_a_fees_usd` / `leg_b_fees_usd` — суммарные комиссии (open + close).
- `entry_spread_pct` / `exit_spread_pct` — для арб-пары: spread на момент открытия/закрытия.
- `opened_at` / `closed_at` — длительность позиции.

**Правило all-legs-closed для пары** (Phase 4.2):
- Для `kind='pair'`: запись попадает в PNL-вкладку ТОЛЬКО когда `status='closed'` И **обе** `leg_a_close_order_id` И `leg_b_close_order_id` IS NOT NULL.
- Сейчас нет промежуточного `status='partially_closed'` — реализовать в Phase 4.3. **Пока:** запись пары со `status='closed'` подразумевает все ноги закрыты; запись пары со `status='open'` не пишется в БД на live path.

**Что нужно добавить в Phase 4** (НЕ сейчас):
- Промежуточный статус `'partially_closed'` или дополнительное поле `legs_closed` (int 0|1|2).
- Правило отрисовки в PNL-вкладке: для пары — показывать ТОЛЬКО `kind='pair', status='closed', leg_a_close_order_id IS NOT NULL, leg_b_close_order_id IS NOT NULL`. Для single — без условия.
- В UI: `partially_closed` пары попадают в Pending-секцию с пометкой «1 of 2 legs closed».

**Текущая дыра** (для Phase 1/4):
- Все Python `close_position` возвращают `realized_pnl_usd: 0.0` hardcoded — реальный realized PNL никогда не пишется из close response. Phase 4.4 должна пуллить `userTrades` после close и пересчитать `leg_*_realized_pnl_usd` + `realized_pnl_usd` из реальных fills. `fills_backfill_service` уже это умеет — нужно вызвать его post-close.

---

## PHASE 1 — ДОСТОВЕРНОСТЬ (баги, ДО любых фич)

Это не фичи — это исправление того, что уже сейчас показывает неверные деньги.

### 1.1 Mark из cache.Store + локальный расчёт UPNL
- **Что:** mark_price для открытой пары брать из `cache.Store` (живой BBO, 20-37/с), UPNL считать
  локально `(mark − entry) × size × side` на каждое обновление mark. entry/size/side — из venue
  (меняются редко). funding_pnl — из venue.
- **Зачем:** сейчас `unrealized_pnl_usd` берётся готовым от venue API → на тихой паре mark
  отстаёт минутами. Требование «максимально свежая MARK/UPNL» так не выполнить.
- **Связка:** привязать к tiered Class 2 — открытая пара уже в hot-set → mark event-driven → UPNL свежий.
- **Флаг:** `AVALANT_LOCAL_UPNL`.
- **Проверить (нужен P3):** открыть позицию, двигать рынок → UPNL на экране меняется на каждом
  тике BBO, не раз в 10с. Сверить локальный UPNL с venue-значением (расхождение < пары $ от funding/комиссий).

### 1.2 Синхронизация ног арб-пары
- **Что:** обе ноги пары брать из ОДНОГО mark-tick (один снапшот cache.Store для обоих символов).
  Если одна нога стейл — помечать пару `stale`, не показывать фейковую разницу.
- **Зачем:** сейчас `tPnl = lp + sp` суммирует ноги из разных моментов (long WS 3с, short REST 10с)
  → разница = шум рассинхрона, а это твой КЛЮЧЕВОЙ показатель.
- **Проверить (P3):** арб-пара на спокойном рынке → разностный PNL не дёргается от рассинхрона;
  на движении обе ноги двигаются одновременно.

### 1.3 Funding за пределами 7 дней
- **Что:** расширить/пагинировать income-историю с момента открытия позиции (entry timestamp),
  не фикс-7-дней. Кэш оставить.
- **Зачем:** funding-арб = accumulated funding это основная прибыль; позиции >7 дней занижают её.
- **Проверить (P3):** позиция старше 7 дней показывает полный funding_pnl, сверить с venue UI.

### 1.4 Стейл-комменты и SWR-окно
- **Что:** поправить неточные комменты (mark «from funding WS» — неверно; таймаут «6-9s» — на деле 5с).
  Пересмотреть SWR 30мин: для торговой страницы 30-мин stale опасен (юзер торгует по старому).
- **Зачем:** доверие к цифре на торговой странице; 30мин stale + торговля = риск решения по старью.
- **Проверить:** stale-индикатор `X-Positions-Stale` реально показывается на фронте; юзер видит,
  что цифра не realtime.

---

## PHASE 2 — Арб-пары: детект + sync-режим

### 2.1 Авто-детект арб-пар
- **Что:** серверная логика «2 позиции = арб-пара»: один токен, противоположные стороны
  (long/short или spot/short), размеры токенов ±одинаковые (с допуском, напр. ±2-5%).
- **Зачем:** сейчас группировки нет, фронт просто суммирует. Нужно понятие пары на сервере.
- **Проверить (P3):** открыть long на бирже A + short на B одного токена ≈равного размера →
  система распознаёт их как пару автоматически.

### 2.2 Sync-режим (ручной выбор)
- **Что:** юзер сам выбирает из своих позиций, какие 2 образуют арб-пару (UI есть → backend).
  Сохранять выбранную связку.
- **Зачем:** автодетект не всегда угадает (несколько позиций на токене, разные размеры).
- **Проверить (P3):** юзер вручную связал 2 позиции → пара отображается как связка, PNL считается разностно.

### 2.3 Единый формат строки
- **Что:** колонки SYMBOL / EXCHANGE / SIDE / SIZE / ENTRY / MARK / FUNDING / P&L / UPNL / UPNL%
  работают и для односторонней позиции, и для арб-пары (где значения = разница/агрегат ног).
- **Проверить (P3):** одиночная позиция и арб-пара рендерятся одним форматом, цифры осмысленны для обоих.

### 2.4 Дисбаланс размеров ног
- **Что:** если размеры ног не равны (частичное заполнение, разный leverage) — корректно считать
  разностный PNL и показывать дисбаланс, а не молча врать.
- **Проверить (P3):** ноги разного размера → UI показывает дисбаланс, PNL считается на меньший общий размер.

---

## PHASE 3 — Закрытие (одиночное + пара, perp + spot)

Каждый провайдер прописывается отдельно (по матрице 0.1).

### 3.1 Закрытие одиночной perp-позиции
- **Что:** довести close_position по всем venue, где backend слабый/не реализован (из матрицы 0.1).
- **Проверить (P3):** реально закрыть маленькую perp-позицию на каждой поддерживаемой бирже.

### 3.2 Закрытие spot-ноги
- **Что:** spot-закрытие ≠ perp (другой механизм — рыночная продажа актива, не close-position).
  Прописать для venue со spot.
- **Зачем:** арб-пара spot/short требует закрыть И spot, И perp разными путями.
- **Проверить (P3):** закрыть spot-ногу реально.

### 3.3 Закрытие арб-пары целиком
- **Что:** закрыть обе ноги (perp+spot или long+short). Обработать частичное закрытие: одна нога
  закрылась, вторая упала → не оставлять юзера в «полупозиции» молча, ретрай/алерт.
- **Проверить (P3):** закрыть арб-пару одной кнопкой → обе ноги закрыты; эмулировать сбой одной ноги
  → система сообщает и не врёт что всё закрыто.

### 3.4 Пост-закрытие: реконсиляция состояния
- **Что:** после close — `invalidate` + дождаться, пока venue реально уберёт позицию (delays
  200-1500мс у некоторых). Не показывать «позиции нет» преждевременно (lastgood покрывает).
- **Проверить (P3):** после закрытия позиция исчезает корректно, без мелькания «нет позиции».

---

## PHASE 4 — Вкладка PNL (закрытые позиции)

### 4.1 Односторонняя закрытая → PNL позиции
- **Что:** закрылась одиночная позиция → сразу показать реализованный PNL в PNL-вкладке.
- **Проверить (P3):** закрыть одиночную → запись с realized PNL появляется.

### 4.2 Арб-пара → разница, ТОЛЬКО когда ВСЕ ноги закрыты
- **Что:** для арб-пары показывать разностный реализованный PNL только после закрытия ВСЕХ ног.
- **Проверить (P3):** закрыть обе ноги → запись с разностным PNL.

### 4.3 Частичное состояние → НЕ показывать PNL
- **Что:** одна нога закрыта, вторая открыта → PNL пары НЕ показывается (pending), помечается
  «ожидает закрытия второй ноги».
- **Зачем:** показать PNL по одной ноге арба = бессмысленная/пугающая цифра (она огромная без хеджа).
- **Проверить (P3):** закрыть одну ногу из пары → PNL-вкладка показывает «pending», не цифру.

### 4.4 Реализованный PNL + accumulated funding, по провайдерам
- **Что:** realized PNL (из venue trade/income history) + полный accumulated funding за время
  жизни позиции. Персистить в БД (closed_positions).
- **Проверить (P3):** сверить realized PNL записи с venue UI; funding полный (не обрезан 7 днями).

---

## PHASE 5 — Вишенки (polish, после рабочего ядра)

| Вишенка | Что даёт |
|---------|----------|
| Freshness-индикатор на каждой строке | Юзер видит, свежий ли mark (live/stale/Xс назад) |
| Оптимистичный close UI | Кнопка close → мгновенный визуальный отклик, реконсиляция фоном |
| История PNL + экспорт CSV | Юзер выгружает закрытые арб-сделки для учёта/налогов |
| Алерт на дисбаланс хеджа | Если ноги разъехались по размеру/одна нога закрылась — предупредить |
| Суммарная статистика | Total realized PNL, win-rate по арб-парам, средний holding time |
| Сортировка/фильтр позиций | По UPNL, по бирже, по возрасту, открытые/закрытые |
| Метрики Prometheus для позиций | rest_fallback_total, ws_liveness, close_success/fail по venue |
| Funding-прогноз | Сколько funding накопится при текущей ставке (для решения держать/закрыть) |

---

## ТЕСТ-ПРОТОКОЛ (обязательно для каждой задачи Phase 1+)

1. **Только на реальной позиции (P3).** Пустая БД даёт ложные результаты (401 → «FAST»).
2. Открыть маленькую тест-позицию (или арб-пару) → проверить отображение → закрыть → проверить
   PNL-вкладку. Полный жизненный цикл, не только чтение.
3. Сверять локальные числа с venue UI (UPNL, funding, realized) — расхождение объяснимо
   (комиссии/funding), не произвольное.
4. Регрессия: не сломалось ли отображение односторонних позиций при добавлении арб-логики.
5. done только с подтверждением на живой позиции в STATUS.md. «Тесты зелёные» ≠ done (см. binance BBO).

---

## ПОРЯДОК ИСПОЛНЕНИЯ (зависимости)

```
P1 (nginx/TLS) ──┐  можно сразу, без кредов
Phase 0 (диагноз)─┴─→ read-only, без кредов
                          │
P2 (TG+user) ────────────┤  твои креды
P3 (кошелёк+позиция) ─────┴─→ открывает живую верификацию
                          │
Phase 1 (достоверность) ──→ ДО фич, на живой позиции
Phase 2 (арб-пары) ───────→ после 1
Phase 3 (закрытие) ───────→ после 0.1 матрицы + 2
Phase 4 (PNL-вкладка) ────→ после 3 (нужны закрытые позиции)
Phase 5 (вишенки) ────────→ после рабочего ядра 1-4
```

**Что можно делать ПРЯМО СЕЙЧАС без кредов:** P1 (nginx), вся Phase 0 (матрицы + дата-модели),
рефактор Phase 1.1/1.2/1.3 за флагом (но БЕЗ приёмки — приёмка ждёт P3).

**Что ждёт твоих кредов:** P2, P3, и вся живая верификация Phase 1-4.

---

## МАТРИЦА ГОТОВНОСТИ ПО ПРОВАЙДЕРАМ (заполнять по ходу)

Колонки:
- **Pos: WS/REST** — источник позиций (user_streams/ файл есть = WS-short-circuit при LIVE; иначе REST).
- **Perp close (Go)** — `ClosePosition` в `go-fetcher/internal/trade/<venue>/<venue>.go`. ✓ = реализован. Используется когда venue в `GO_TRADE_VENUES` (сейчас все 17 CEX+DEX, кроме ethereal). Python fallback включается при transient/internal Go-ошибке.
- **Perp close (Python)** — `close_position` в `backend/services/trade_adapters/<venue>.py`. ✓ = reduce-only market. Используется когда venue НЕ в GO_TRADE_VENUES или Go fallback.
- **Spot close (Go)** — `CloseSpotPosition` в `go-fetcher/internal/trade/<venue>/spot.go`. ✓ = реализован, route'ится через `market_type=spot`. ✗ = venue без spot.
- **Spot close (Python)** — отсутствует у всех CEX в Python (нет dedicated метода); backpack — исключение (Python `close_position` = spot-sell).
- **UPNL local** — отложено до Phase 1.1 (везде `?`).
- **Funding full** — отложено до Phase 1.3 (везде `?`).

| Venue | Pos: WS/REST | Perp close (Go) | Perp close (Python) | Spot close (Go) | Spot close (Python) | UPNL local | Funding full | Заметка |
|-------|---|---|---|---|---|---|---|---|
| binance | WS | ✓ binance.go:484 | ✓ hedge-aware reduceOnly market (binance.py:329) — realized_pnl_usd=0 hardcoded | ✓ spot.go:215 | ✗ | ? | ? | |
| bybit | WS | ✓ bybit.go:484 | ✓ reduceOnly market (bybit.py:343) | ✓ spot.go:125 | ✗ | ? | ? | |
| okx | WS | ✓ okx.go:478 | ✓ posSide-aware (okx.py:339) | ✓ spot.go:75 | ✗ | ? | ? | |
| gate | WS | ✓ gate.go:421 | ✓ (gate.py) | ✓ spot.go:69 | ✗ | ? | ? | |
| bitget | WS | ✓ bitget.go:434 | ✓ | ✓ spot.go:105 | ✗ | ? | ? | |
| kucoin | WS | ✓ kucoin.go:400 | ✓ | ✓ spot.go:248 | ✗ | ? | ? | |
| mexc | WS | ✓ mexc.go:431 | ✓ | ✓ spot.go:144 | ✗ | ? | ? | mexc REST geo-blocked from prod IP |
| bingx | WS | ✓ bingx.go:408 | ✓ | ✓ spot.go:63 | ✗ | ? | ? | |
| htx | WS | ✓ htx.go:375 | ✓ | ✓ spot.go:210 | ✗ | ? | ? | |
| kraken | WS | ✓ kraken.go:274 | ✓ (kraken.py:228) | ✓ spot.go:182 | ✗ | ? | ? | |
| backpack | WS | ✓ backpack.go:277 | ⚠ Python close_position = SPOT-sell (backpack.py:245). При fallback на Python для perp — закроет спот, не perp. | ✓ spot.go:17 | ✓ via Python (наследие) | ? | ? | dispatch trap: Python close ≠ perp |
| whitebit | WS | ✓ whitebit.go:221 | ✓ (whitebit.py:241) | ✓ spot.go:136 | ✗ | ? | ? | |
| hyperliquid | WS | ✓ hyperliquid.go:560 | ✓ | ✓ spot.go:190 | ✗ | ? | ? | |
| aster | WS | ✓ aster.go:364 | ✓ (aster.py:231) | ✗ | ✗ | ? | ? | perp-only venue |
| lighter | WS | ⚠ lighter.go реализован, но **runtime errZK** при close (CGO sdk не работает в Go) | ⚠ Python adapter ошибается при отсутствии account_index | ✗ | ✗ | ? | ? | geo-IP blocked + close path сломан |
| paradex | REST | ✓ paradex.go:542 | ✓ (paradex.py:232) | ✗ | ✗ | ? | ? | perp DEX, нет user-stream |
| ethereal | REST | ✓ ethereal.go:293 | ✓ (ethereal.py:304) | ✗ | ✗ | ? | ? | НЕ в GO_TRADE_VENUES → всегда Python |
| extended | REST | ✓ extended.go:671 | через `trade_proxy.close_position("extended", ...)` (extended.py:115) | ✗ | ✗ | ? | ? | Python — proxy-only shim |

### Дыры/риски на близкое рассмотрение (для Phase 3 закрытие)

1. **backpack dispatch trap**: если Go fallback'нется на Python для perp backpack, Python `close_position` продаст СПОТ-баланс. Решение в Phase 3: или гарантировать что backpack никогда не уходит на Python для perp, или переписать backpack.py:close_position в perp-режим.

2. **realized_pnl_usd = 0.0 в close response**: все Python адаптеры возвращают захардкоженный 0. Реальный realized PNL нужно тянуть из последующего `userTrades`/`fills` запроса. Phase 4 будет считать realized PNL отдельно — `close` response — лишь подтверждение факта закрытия.

3. **lighter close сломан с двух сторон**: Go возвращает `errZK` (Schnorr signer не работает в чистом Go без CGO), Python — `Lighter requires account_index`. Phase 3.1 ставит lighter в `blocked` до отдельной работы по signer'у.

4. **5 venues без spot close**: aster, ethereal, extended, lighter, paradex — perp-only. Spot/short арб-пары на этих venue невозможны (long-leg должна быть на CEX со спотом). Phase 2 авто-детект пар должен это учитывать.

5. **ethereal не в GO_TRADE_VENUES** → trade_service всегда зовёт Python adapter. Если ethereal перенесётся в Go, поменять `.env`.

Заполняется в Phase 0.1/0.2 и обновляется по мере реализации.
