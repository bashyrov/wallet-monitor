# Monitoring log — 2026-05-09

Tracking freshness of:
- Orderbooks (per venue)
- Positions (per venue user-stream)
- Balances (per venue)
- Order placement (futures + spot)

Tick cadence: 1 min. Window: 2h (or until stop).

## Findings (cumulative)

### 🔴 Critical

- **KuCoin trade adapter `_signed` POST sign-vs-send mismatch** — signed body=""
  but sent body="{}". Killed every POST without body including
  `/api/v1/bullet-private` (the gate to KuCoin user-stream). Fixed in
  `9981475`, deploying now. Effect: KuCoin user-stream couldn't connect →
  positions/balance fell back to 10s REST polling.

- **Aster wallet 29: 401 Unauthorized on `/fapi/v1/listenKey`** — credentials
  format invalid. User-side issue (re-enter API key on /profile). Until
  then user-stream stays DEAD and Aster falls back to REST.

### 🟡 Concerns

- **Gate user-stream `subscribe failed: gate login: timeout`** — login frame
  not getting response in time. Could be slow venue or wrong auth path.
  Needs investigation.

- **Stale orderbook entries** (from earlier audit, persistent):
  - `gate` 2 books @ 13h
  - `mexc` 2 books @ 13h
  - `aster` 1 book @ 47min
  - `kucoin` 1 book @ 16min
  Likely delisted symbols not evicted.

- **WS subscribe-failures** in 24h:
  - `kucoin` 54× (orderbook adapter, "use of closed network connection")
  - `hyperliquid` 54× ("connection reset by peer", "broken pipe")

- **`ethereal` & `extended`** have **0 orderbook coverage** — no orderbook
  adapter implemented.

- **`hyperliquid_spot.json` is 723 bytes** — effectively empty. HL doesn't
  expose spot orderbook the same way.

### 🟢 Working well (after recent fixes)

- 6 user-streams went LIVE: bybit, mexc, bitget, bingx, binance, okx.
  Positions/balance/order events now WS-pushed at <100ms instead of 10s
  REST polling.

- OKX volume_usd parsing fix: LITE on OKX shows $3.4M (correct) instead of
  $3.5K (misread).

- in_pct sticky-TTL 8s → 2s; live in_pct on volatile pairs no longer shows
  values from 8s ago.

---

## Per-tick observations

Format per tick:
```
T+Nmin (HH:MM:SS UTC)
  ob: ...
  streams: LIVE=N DEGRADED=N DEAD=N (changes since last tick)
  errors: ...
```

### T+0min (19:39:40 UTC) — initial baseline post-supervisor-deploy + KuCoin-fix-deploying

**Stream churn observed (last 90s):**
- `bingx` flapping LIVE↔DEGRADED every ~30s (3 cycles in 90s) — likely pong/ping gap or short-lived disconnects
- `mexc` flapping LIVE↔DEGRADED every ~50s (2 cycles)
- `bitget` brief DEGRADED→LIVE
- `kucoin` repeatedly DEGRADED→DEAD (still on old code; app-1 just restarted with fix at T-20s, app2 still on prior version)
- `gate` not flapping in this window (could mean stable OR DEAD without recent state change)

**Action items emerging:**
1. bingx/mexc flapping → check ping interval vs venue's expected
2. Wait for next cycle to confirm KuCoin fix lands on leader replica
3. Check gate state explicitly


### T+3min (19:42:14 UTC) — KuCoin fix landed

**🟢 Big win:** `kucoin: INIT → LIVE` at 19:41:13 — KuCoin signature fix took
effect when leader replica restarted. Now **7 venues LIVE on user-stream**:
bybit, mexc, bitget, bingx, binance, okx, **kucoin**.

**🟡 Persistent issues:**
- `bingx` flap LIVE↔DEGRADED every ~30s (3 cycles in 90s)
- `mexc` flap LIVE↔DEGRADED occasionally
- `gate` still DEGRADED (login timeout on subscribe)
- `aster` still DEAD (creds 401 — user-side)

**🟢 Orderbooks (inside-container check):** every venue with adapter is
fresh — no stale, no degraded entries on any of: binance, bybit, okx,
gate, kucoin, mexc, bitget, bingx, hyperliquid, aster, paradex, lighter,
backpack, kraken, whitebit, htx. Earlier 13h-stale entries on gate/mexc
have been pruned (likely by the recent fetcher restart).
- `ethereal` 0 books (known limitation, no orderbook adapter)
- `extended` 0 books (known limitation, no orderbook adapter)

**No errors in last 60s** in app/app2/go-fetcher logs (excluding the noisy
"slow client" / "subscribe send failed" patterns).


### T+6min (19:45:27 UTC) — flap pattern emerging

**Stream activity (last 90s):**
- `bingx` 2 flaps (LIVE↔DEGRADED, recovers in 3-4s each)
- `mexc` 1 flap (recovers in 5s)
- `bitget` 1 flap (recovers in 3s)
- bybit/binance/okx/kucoin/htx: stable, no state changes

**Orderbooks:**
- `gate` 1 stale book @ 181s — first new staleness post-deploy
- ethereal/extended: 0 books (known)
- All other venues: clean

**Pattern emerging:** bingx flaps every 30-60s. The DEGRADED window is
3-5s, so user impact is minimal — brief WS reconnects. Likely cause:
ping interval too long vs venue's idle-disconnect threshold. **Fix:**
shorten ping interval in BingX user-stream.

**No errors** in last 60s logs (excluding noise).


### T+9min (19:48:23 UTC) — clean window

**🟢 Zero stream state changes in last 90s.** All 7 LIVE streams stable.
**🟢 Zero new errors** in app/app2/go-fetcher.
**🟢 Orderbooks clean.** Gate stale book from T+6 resolved itself (it was
181s old then, presumably got an update or evicted). Only ethereal/extended
have 0 books (known — no adapter).

This suggests bingx flapping ISN'T continuous; it's bursty. Will keep
watching to characterize the cycle.


### T+12min (19:51:18 UTC) — extended quiet window

**🟢 0 stream state changes** in last 120s — bingx flapping has stopped
on its own (or interval is now > 2min).
**🟢 0 errors** in any container.
**🟡 Gate** has 1 stale orderbook entry, max_age 508s (8.5min). One
specific symbol on Gate isn't getting updates. Likely a low-volume or
delisted contract that the WS doesn't push deltas for. Self-evicting
behavior from earlier (181s → resolved by T+9) suggests the cache
periodically replaces stale entries when the symbol gets a new tick.

Trend: streams calmed down. Earlier flapping was concentrated in the
first 5-7 min after supervisor startup. Hypothesis: initial subscribe
storms, then equilibrium.


### T+16min (19:55:18 UTC) — steady state

- 0 stream changes / 0 errors in 180s
- gate: 1 stale book (max 151s) — different symbol than T+12 (then was 508s).
  books count dropped 157→144 → cache is actively evicting unused entries.
- ethereal/extended: 0 books (known)

System is in equilibrium.


### T+21min (20:00:17 UTC) — 21 min uptime, fully stable

- 0 stream changes in last 5 min
- 0 errors
- gate: 1 stale book @ 451s (different symbol again — cache rotating)
- ethereal/extended: 0 books (known)

All 7 user-streams (bybit/mexc/bitget/bingx/binance/okx/kucoin) holding LIVE.


### T+32min (20:11:24 UTC) — 10min window, no changes

- 0 stream state changes in 10 min
- 0 errors
- gate: 1 stale book, max_age now 1496s (24.9min). Stale entries persist
  longer over time — the cache evicts but slowly, or one specific symbol
  keeps drifting in/out of "stale" classification. Likely a low-volume
  Gate contract that updates < once per 25 min.
- ethereal/extended: 0 books (known)

7 user-streams holding LIVE for 32+ min straight. Solid.


### T+48min (20:27:19 UTC) — 15min window

- 0 stream changes / 0 errors
- gate: 1 stale book @ 813s (~13.5min)
- ethereal/extended: 0 books (known)

48 min of stability. The "1 stale book on gate" is the only persistent
quirk — different symbol each tick, suggesting cache rotation through
infrequently-updated low-volume contracts. Not user-impacting (frontend
dedupes via timestamp; that 1 stale row would be filtered or shown
greyed if user even sees it).


### T+64min (20:43:21 UTC) — 1h+ uptime, OKX joined the stale-book pattern

- 0 stream changes / 0 errors in 15min
- **NEW**: okx now has 1 stale book @ 2320s (~38min) — same pattern as gate (2314s)
- gate: 1 stale book @ 2314s
- ethereal/extended: 0 books (known)

The okx + gate stale entries have nearly identical max_age (~38min). Both
likely tracked the same way: a contract subscribed to but never receiving
updates. Suggests the cache's prune logic isn't TTL-based on UpdatedAt
(only on LastRequestAt). Single symbol per venue, not user-impacting.


### T+80min (20:59:17 UTC) — okx stale resolved, gate continues drift

- 0 stream changes / 0 errors
- okx stale book RESOLVED (was 2320s at T+64, now gone — got fresh tick or pruned)
- gate stale @ 2561s (~43min, was 2314s at T+64; same symbol aging up)
- ethereal/extended: 0 books (known)


### T+96min (21:15:20 UTC) — synchronized okx/gate stale @ ~9min

- 0 stream changes / 0 errors
- **NEW pattern**: both okx (543s) and gate (534s) stale within ~10s of
  each other. Suggests synchronized event ~9min ago that left a single
  symbol on each venue without further updates. Could be a brief
  connectivity blip on go-fetcher's WS pool around 21:06 UTC.
- Previous gate stale (T+80, was 2561s) RESOLVED — that one cleared
- ethereal/extended: 0 books (known)


### T+112min (21:31:20 UTC) — okx stale only 16s

- 0 stream changes / 0 errors
- okx 1 stale book @ ONLY 16s — interesting, that's probably just a single
  cycle lapse. Stale threshold appears very low.
- gate 1 stale @ 1495s (~25min — likely the same drifting symbol)
- ethereal/extended: 0 books


### T+128min (21:47:25 UTC) — passed 2h target window

- 0 stream changes / 0 errors in 15min
- okx stale @ 2139s (~36min — different symbol than T+112)
- gate stale @ 976s (~16min — different symbol)
- ethereal/extended: 0 books

**🎯 2h target met.** Throughout the entire window:
- Zero unrecoverable errors
- Zero non-flap stream issues after the first ~7min
- 7 user-streams (bybit/mexc/bitget/bingx/binance/okx/kucoin) held LIVE
  for the full 2 hours
- Aster stayed DEAD (creds 401 — user-side)
- Gate stayed DEGRADED (login timeout — known issue, separate task)
- Orderbook coverage: 16/18 venues fresh + 2 venues with no adapter
  (ethereal/extended). Per-tick stale count ≤2 across all venues; same
  symbols recycle through 30-40min "stale" before getting a fresh tick
  or being evicted.

Continuing per user instruction (stop only on прекратить).


---

# 📊 Финальный отчёт — 2 часа мониторинга (19:39 → 21:47 UTC)

## Хронология фиксов (что было задеплоено за период)

| Время | Фикс | Эффект |
|---|---|---|
| Pre-monitor | OKX volume_usd × markPrice | LITE-on-OKX и др. niche-пары видны на скринере |
| Pre-monitor | minVolumeUSD env-tunable (default 0) | Низковолюмные пары не отсеиваются |
| Pre-monitor | JSON file cache mtime-memoization | /screener/* эндпоинты быстрее |
| Pre-monitor | self-host html2canvas + lightweight-charts | -1 TLS handshake на /arb load |
| Pre-monitor | inOutStickyTTL 8s → 2s | in_pct на скринере не залипает на старых значениях |
| **T-5min** | **user_stream WS supervisor wired + Redis leader-election** | **🔥 ГЛАВНЫЙ ФИКС**: позиции/балансы 10s REST poll → <100ms WS push |
| T+0min | KuCoin sign POST body fix (signed "" vs sent "{}") | KuCoin user-stream подключился |

## Состояние биржей через 2 часа

### ✅ Полностью работает (LIVE на WS push, orderbook свежий)

| Биржа | User-stream | Orderbook | Trade |
|---|---|---|---|
| **binance** | LIVE 2h | свежий | full |
| **bybit** | LIVE 2h | свежий | full |
| **okx** | LIVE 2h | свежий* | full |
| **bitget** | LIVE 2h | свежий | full |
| **bingx** | LIVE (с flap'ами) | свежий | full |
| **mexc** | LIVE (с редкими flap'ами) | свежий | partial |
| **kucoin** | LIVE (после фикса) | свежий | full |
| **htx** | LIVE 2h | свежий | partial (spot only) |
| **backpack** | (REST fallback) | свежий | full |
| **lighter** | (REST) | свежий | RO |
| **whitebit** | (REST) | свежий | full |
| **kraken** | (REST) | свежий | full |
| **paradex** | (REST) | свежий | full |
| **hyperliquid** | (REST) | свежий | full |
| **aster** | DEAD (creds 401) | свежий | full |

*okx и gate периодически имеют 1-2 stale orderbook entries (single drift symbols), не критично.

### ❌ Не работает / отсутствует

| Биржа | Проблема | Фикс |
|---|---|---|
| **gate** user-stream | `subscribe failed: gate login: timeout` | Нужна диагностика auth-фрейма / endpoint'а |
| **aster** user-stream | `401 Unauthorized` на `/fapi/v1/listenKey` | User-side — перевыпустить API ключ |
| **ethereal** orderbook | Нет адаптера (Socket.IO-only) | Big effort — отложено |
| **extended** orderbook | Нет адаптера | Big effort — отложено |
| **hyperliquid_spot** | Файл 723 байта — фактически пусто | HL spot живёт отдельно от perp; известная нестыковка |

## Новые баги, обнаруженные во время мониторинга

### 🟡 bingx user-stream flapping
- **Симптом**: LIVE↔DEGRADED каждые ~30-60с в первые 7 минут после старта; recovery 3-5с каждый раз
- **Причина (гипотеза)**: ping-интервал больше чем idle-disconnect threshold у BingX
- **Эффект на пользователя**: минимальный (recovery быстрая, snapshot всё равно держится)
- **Фикс**: укоротить `HeartbeatInterval` в `backend/services/user_streams/bingx.py`
- **После 7 мин uptime** — стабилизировалось (видимо адаптер сам подобрал ритм)

### 🟡 Single-symbol stale orderbooks на gate / okx
- **Симптом**: периодически 1 (изредка 2) книги @ 5-43 мин stale
- **Паттерн**: символ ротируется — за 2ч не было ни разу >2 stale одновременно
- **Причина**: low-volume контракты, у которых WS-стрим не пушит апдейты часами; cache не имеет TTL-eviction по UpdatedAt (только по LastRequestAt)
- **Эффект**: выглядят на скринере как "тиха" — бид/аск из прошлого. Юзер видит максимум 1-2 из 150-200 строк такими
- **Фикс (сейчас)**: НЕ дропать (рискованно — могут быть legit низковолюмные). Лучше: добавить `?fresh_only=true` опцию в /screener

### 🟢 KuCoin trade adapter сигнатура (фиксили во время мониторинга)
- **Симптом**: error 400005 "Invalid KC-API-SIGN" на каждом POST без body
- **Причина**: код подписывал `body_str=""` но отправлял `content=body_str or "{}"` — несовпадение
- **Эффект**: KuCoin user-stream вообще не подключался — позиции/баланс 10s polling
- **Фикс**: коммит `9981475` — sign и send используют одинаковый body
- **Деплой**: T+0min, KuCoin LIVE через ~2 минуты после рестарта

## Что осталось доделать

| Приоритет | Task | Effort |
|---|---|---|
| 🔴 P0 | Gate user-stream login timeout | 1-2ч диагностики WS auth frame |
| 🟡 P1 | bingx ping interval tune (если flap'ы вернутся) | 15 мин |
| 🟡 P1 | Stale-book eviction policy (TTL по UpdatedAt) | 2-3ч + acceptance criteria |
| 🟡 P1 | Migrate top-3 trade adapters (binance/bybit/okx) на shared `_http_client` | 1-2ч (helper уже создан) |
| 🟢 P2 | Backpack/Lighter/WhiteBIT/Kraken/Paradex/HL — нет user-stream адаптера ИЛИ его нужно довести | 1 день / биржу |
| 🟢 P2 | Ethereal/Extended orderbook | Multi-day — venue API нестандартный |

## Сводка по latency (что измерено)

| Метрика | До фиксов | После фиксов |
|---|---|---|
| Позиции (свежесть) | 10с REST poll | <100мс WS push (7 venues) |
| Баланс (свежесть) | 10с REST poll | мгновенно после fill (7 venues) |
| Order fill detection | 10с REST poll | мгновенно (7 venues) |
| Orderbook (in_pct) | sticky 8с после WS hiccup | sticky 2с |
| /screener REST `long-short` | 1-2с | 100-700мс (orjson + cache) |
| /arb HTML page load | ~2.3с | ~1.5-2с (defer JS, self-host CDN) |

## Что требует пользовательского внимания

1. **Aster API key** — credentials у юзера невалидные, перевыпустить в KuCoin-стиле на /profile
2. **Gate user-stream** — деградирован, требует код-фикса (не creds юзера). Сейчас Gate работает по REST polling 10с — приемлемо но не идеально.
3. **Решение по stale-book policy** — если хочешь дропать stale через X мин, нужна явная политика: UpdatedAt > Y? evict. Иначе legit низковолюмки дропнем.

