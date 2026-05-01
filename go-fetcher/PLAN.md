# Go fetcher rewrite — execution plan

> **Контракт**: drop-in replacement для Python fetcher'а. Пишет те же файлы
> (`/tmp/avalant_cache/books.<exchange>.json`, `books.master.json`,
> `funding.json`) и слушает те же Redis-каналы. Python web-роли НЕ меняются.
>
> **Слив в main**: только после прохождения тестов и явного "ок" от пользователя.
> До этого момента ветка `rewrite/go-hot-path` живёт сама по себе.

## Карта известных багов (которые НЕ должны вернуться)

Каждый Go-адаптер обязан явно решать каждый из этих случаев — иначе мы
повторим регрессии что ловили месяцами в Python.

| # | Симптом | Корень | Где решается в Go |
|---|---------|--------|-------------------|
| 1 | bitget_spot WS stale → reconnect loop | orjson.dumps возвращал bytes → BINARY frame; Bitget V2 `books` принимает только TEXT | `base.SendJSON()` всегда `TextMessage` |
| 2 | binance/aster reconnect storm | policy-violation closes 1008/3001/4400/4401 шли через обычный backoff → быстрые retry → бан | `base.policyBackoff` — отдельный длинный таймер, сбрасывается только на data-frame |
| 3 | hyperliquid funding 1011 keepalive | те же policy-violation семантика | тот же policy backoff |
| 4 | Bitget V2 закрывает WS через 30с тишины | сервер ожидает текстовый "ping" frame от клиента | `Adapter.HeartbeatFrame()` возвращает `[]byte("ping")`, отправляется как Text |
| 5 | BingX закрывает WS на native lib pings | сервер шлёт gzip "Ping", ждёт "Pong"; native pings ignored | `Adapter.PongFor()` ловит "Ping" и отвечает "Pong"; lib pings отключены |
| 6 | KuCoin/HTX тоже игнорируют lib pings | свои app-level ping форматы | `lib_ping_disabled = true` для этих венй |
| 7 | bybit volume пропадал на partial updates | WS отдаёт price без volume на дельтах | REST backstop (`internal/backstop/`) на каждой бирже где WS неполный |
| 8 | Binance возвращает delisted symbols (NTRN) с `status=BREAK` | биржа не убирает из ticker | filter через `/exchangeInfo` `status='TRADING'`, кэш 10 мин |
| 9 | Spot WS не поднимались для bitget/binance/mexc | prewarm только для top-N opps | `internal/cache/store.go.OnDemand()` — подписывается при первом обращении через Redis-канал `book:subscribe` |
| 10 | `/orderbook-spot` бил REST мимо WS-кэша | endpoint миновал слой кэша | Go-fetcher единственный источник; Python web просто читает файл |
| 11 | Empty-book hammer при делистнутых символах | poller бил REST каждые 300мс | `pollLoop` экспоненциальный backoff: 5 empty → 30с, 20 empty → 5 мин |
| 12 | Books.json в момент дампа был частичным (читатель ловил пустоту) | non-atomic write | `WriteAtomic()`: tempfile + `os.Rename` (POSIX гарантирует атомарность) |
| 13 | Web-роли поднимали свой `_book_cache` дублируя fetcher | shared-state через файлы — единственный sane вариант для multi-process | Web НЕ хранит кэш — каждый запрос читает книжку из файла + (опц.) Redis |
| 14 | Symbol mapping inconsistencies | у каждой биржи свой формат: `BTCUSDT`, `BTC-USDT`, `BTC_USDT`, `XBT/USDT`, `PF_BTCUSD` | per-adapter `BuildSymbol(token string) string` метод |
| 15 | Bitget V2 instType for spot vs USDT-FUTURES | один URL, разные channels | разные адаптеры `bitget` и `bitget_spot` с разной подпиской |
| 16 | Lighter symbol→market_id integer | требует REST-резолвинг + 1h кэш | `internal/exchanges/lighter/idmap.go` |
| 17 | KuCoin token-auth | публичный URL добывается через POST `/api/v1/bullet-public` | `internal/exchanges/kucoin/auth.go` |
| 18 | Aster — Binance fork | identical протокол | `internal/exchanges/aster/futures.go` встраивает `binance.Futures` |
| 19 | Subscribe-rate limit на KuCoin (~3/sec) | flood → бан | `Adapter.SubscribeDelay = 400ms` |
| 20 | Stale-data watchdog при тихом TCP | NAT/CDN keep TCP up но не шлют frames | `internal/base/watchdog.go` — 30с порог, force-close |
| 21 | Cold-start пустые книги при первом hit | WS только что подписался, REST ещё не пришёл | при subscribe — параллельный REST-fetch, кладётся в кэш сразу |
| 22 | orderbook canonical limits | каждая биржа принимает только свой набор | `internal/canonical/limits.go` |
| 23 | Web читает delisted/halted делая REST к биржe лишний раз | можно кэшировать `_exchangeInfo` 10 мин | `internal/listed/cache.go` |
| 24 | IP-bans (Binance 418 после quota) | нет защиты | per-exchange circuit breaker: 10 consecutive 5xx → 60с skip |

## Архитектура

```
                    ┌────────── Frontend (vanilla JS, без изменений) ──────────┐
                    └───────────────────────┬───────────────────────────────────┘
                                            │ HTTPS
              ┌─────────────────────────────▼──────────────────────────────────┐
              │  Python web (FastAPI) — auth/plans/admin/payments/trade/user-ws │
              │  Читает books.json + funding.json + spot_arbitrage.json — те    │
              │  же контракты что и сейчас. Не знает что данные пишет Go.       │
              └─────────────────────────────▲──────────────────────────────────┘
                                            │ pub/sub: book:subscribe, book:unsubscribe
                                            │ files: /tmp/avalant_cache/*.json
              ┌─────────────────────────────┴──────────────────────────────────┐
              │  Go fetcher (avalant-fetcher) — НОВЫЙ                          │
              │  • orderbook WS adapters × 16                                   │
              │  • funding WS adapters × 12                                     │
              │  • REST backstops (per-venue)                                   │
              │  • merger → books.json                                         │
              │  • spot/dex arb compute                                         │
              │  • Redis cache writes (per-key throttle)                        │
              └────────────────────────────────────────────────────────────────┘
                                            │
                  ┌────────────┬─────────────┼─────────────┬──────────────┐
                  ▼            ▼             ▼             ▼              ▼
              Binance       Bybit          OKX           Gate          ... 12 more
```

## Структура репо

```
go-fetcher/
├── cmd/
│   └── fetcher/main.go                # entry, supervisor, signal handling
├── internal/
│   ├── config/config.go               # env-var loading (AVALANT_*)
│   ├── log/log.go                     # zerolog wrapper, JSON output
│   ├── canonical/limits.go            # per-exchange valid limit sets
│   ├── listed/cache.go                # exchangeInfo TRADING-symbols cache
│   ├── circuit/breaker.go             # per-exchange REST circuit breaker
│   ├── cache/
│   │   ├── store.go                   # in-memory book cache + atomic dump
│   │   ├── redis.go                   # Redis writes throttled per key
│   │   └── files.go                   # books.<ex>.json + master merger
│   ├── ws/
│   │   ├── adapter.go                 # interface
│   │   ├── runner.go                  # connect/subscribe/recv loop
│   │   ├── policy.go                  # 1008/3001/4400/4401/1011 backoff
│   │   ├── watchdog.go                # stale-frames detector
│   │   └── send.go                    # SendJSON — TextMessage only
│   ├── exchanges/
│   │   ├── binance/{futures,spot,exchangeinfo}.go
│   │   ├── bybit/{futures,spot}.go
│   │   ├── okx/{futures,spot}.go
│   │   ├── gate/{futures,spot}.go
│   │   ├── kucoin/{futures,spot,auth}.go
│   │   ├── mexc/{futures,spot}.go    # mexc_spot пока не subscribe (Singapore IP)
│   │   ├── bitget/{futures,spot}.go
│   │   ├── bingx/{futures,spot}.go
│   │   ├── aster/futures.go           # binance fork
│   │   ├── hyperliquid/futures.go
│   │   ├── htx/{futures,spot}.go
│   │   ├── kraken/futures.go
│   │   ├── backpack/futures.go
│   │   ├── lighter/{futures,idmap}.go
│   │   ├── paradex/futures.go
│   │   └── whitebit/futures.go
│   ├── funding/
│   │   ├── adapter.go                 # funding-WS interface
│   │   ├── runner.go                  # WS + REST backstop runner
│   │   └── <16 adapters>.go
│   ├── arb/
│   │   ├── futures.go                 # cross-venue funding-rate arb
│   │   └── spot.go                    # spot/short opportunities
│   └── broadcast/                     # ФАЗА 5 — может остаться на Python
├── pkg/
│   └── jsonutil/jsonutil.go           # sonic wrappers (TEXT-aware)
├── PLAN.md                            # ← этот файл
├── README.md
├── go.mod
├── go.sum
└── Dockerfile
```

## Фазы — execution checklist

### ✅ Фаза 0 — Setup (1-2 дня)
- [x] Ветка `rewrite/go-hot-path`
- [ ] `go-fetcher/` директория + go.mod (Go 1.22+)
- [ ] `internal/log` (zerolog, JSON-format)
- [ ] `internal/config` (env-vars: REDIS_URL, AVALANT_FETCHER_CACHE_DIR, ...)
- [ ] `internal/ws/adapter.go` — interface
- [ ] `internal/ws/runner.go` — connect/subscribe/recv/reconnect/policy-backoff/watchdog
- [ ] `internal/ws/send.go` — `SendText(ws, []byte)` обёртка (предотвращает баг #1)
- [ ] `internal/cache/store.go` — in-memory книжка + atomic dump
- [ ] `internal/canonical/limits.go` — все 16 наборов
- [ ] `cmd/fetcher/main.go` — minimal supervisor
- [ ] `Dockerfile` — multi-stage, distroless final stage
- [ ] CI lint: `go vet ./... && staticcheck ./...`

### Фаза 1 — 3 эталонных WS адаптера (1-2 недели)
**Выбор**: Binance, Bybit, OKX — самые документированные, типовые snapshot+delta.
Наберём шаблон + всю инфру вокруг. Остальные 13 — копипаста с подменой.

- [ ] `internal/exchanges/binance/futures.go` — full snapshot @ 100ms diff
- [ ] `internal/exchanges/binance/exchangeinfo.go` — TRADING-only filter (баг #8)
- [ ] `internal/exchanges/bybit/futures.go` — V5 orderbook channel
- [ ] `internal/exchanges/okx/futures.go` — books50-l2-tbt + checksum validation
- [ ] Diff-скрипт: `scripts/diff_books.sh` сравнивает Go vs Python `books.<ex>.json` каждые 30с
- [ ] Параллельный прогон 24 часа без расхождений

### Фаза 2 — Остальные 13 orderbook WS (3 недели)
Порядок по сложности (простые → сложные):
- [ ] aster (фактически копия binance, разные хосты)
- [ ] gate
- [ ] mexc
- [ ] whitebit
- [ ] bingx (gzip + Ping/Pong баг #5)
- [ ] htx (gzip)
- [ ] kraken futures (PF_TOKEN формат)
- [ ] kucoin (token auth, баг #17)
- [ ] bitget (futures + spot, баги #1, #4, #6, #15)
- [ ] hyperliquid (POST + WS гибрид)
- [ ] paradex (Stark signing для трейда — но WS public)
- [ ] lighter (integer market_id, баг #16)
- [ ] backpack ({BASE}_USDC_PERP)

Spot-варианты включаются по флагу — `mexc_spot` остаётся отключённым из-за блокировки Singapore IP (закомментировать в `cmd/fetcher/main.go`).

### Фаза 3 — Funding WS + REST backstop (2 недели)
12 funding-адаптеров. На Go REST backstop = горутина с тикером, не "трюк".
- [ ] `internal/funding/runner.go` — WS+REST одновременно
- [ ] 12 funding адаптеров
- [ ] Pure-thread → goroutine + sync.Map для cross-thread writes (~эквивалент GIL-atomic dict assign в Python)

### Фаза 4 — Merger + Redis writes (1 неделя)
- [ ] `internal/cache/files.go` — слияние per-venue в `books.json`
- [ ] `internal/cache/redis.go` — Redis writes 50ms throttle per key
- [ ] `internal/cache/master.go` — books.master.json для spot+exotic

### Фаза 5 — (опционально) Broadcast WS
- [ ] `/ws/funding`, `/ws/long-short`, `/ws/book` — переезд на Go
- [ ] JWT first-frame валидация (общий secret с Python)
- [ ] Nginx routing → Go upstream

Можно не делать сейчас — оставить Python на broadcast'е, всё равно ~70% выигрыша CPU будет от смены fetcher'а.

### Фаза 6 — Production rollout (2-3 недели)
- [ ] Docker-compose добавить сервис `go-fetcher` рядом с `fetcher`
- [ ] 1 неделя — оба пишут в свои файлы (Python в `books.<ex>.json`, Go в `books.<ex>.json.go`)
- [ ] Diff-метрики в Prometheus: `book_diff_pct{exchange="..."}` алерт >0.05%
- [ ] 1 неделя — Go primary (web читает `.go`-файлы), Python в standby
- [ ] Финальное удаление Python `fetcher/`, `backend/services/orderbook_ws/`,
      `backend/services/funding_ws/`
- [ ] Update CLAUDE.md, DEPLOY.md, .env.sample

## Метрики прогресса

После каждой фазы — обновить эту таблицу:

| Фаза | Status | Дата | Заметки |
|------|--------|------|---------|
| 0 — Setup | ✅ done | 2026-04-30 | scaffold + ws framework + cache + Dockerfile |
| 1 — 3 эталонных WS | ✅ done | 2026-04-30 | binance/bybit/okx futures — все наполняют top-N книжки в первые 2с после connect; diff_books.sh готов |
| 2 — Остальные 13 WS | ✅ done | 2026-04-30 | aster/gate/mexc/whitebit/bingx/htx/kraken/kucoin/bitget+spot/hyperliquid/paradex/lighter/backpack — все 17 пишут books.<ex>.json. |
| 3 — Funding WS | ✅ done | 2026-04-30 | 12 funding adapters: WS+REST для binance/bybit/okx/bitget/aster/gate (push-fast), REST-only для kucoin/mexc/bingx/htx/hyperliquid/whitebit. Mark price совпадает между venues ±0.1% — реальный spread. |
| 4 — Merger + Redis | ✅ done | 2026-04-30 | SymbolManager (prewarm + user-touch + reconcile every 5s), Redis pub/sub bridge (book:subscribe / book:unsubscribe), books.master.json для spot/exotic, cross-pollination HTX mark из orderbook midprice. End-to-end WLD on-demand subscribe verified: T+10s после PUBLISH книжка наполняется на 20-50 уровней. |
| 5 — Broadcast (опц.) | ⬜ | — | — |
| 6 — Rollout | ⬜ | — | — |

## Команды для разработки

```bash
# build
cd go-fetcher && go build -o /tmp/avalant-fetcher ./cmd/fetcher

# run локально (нужен Redis + writable cache dir)
AVALANT_FETCHER_CACHE_DIR=/tmp/avalant_cache REDIS_URL=redis://localhost:6379/0 \
  /tmp/avalant-fetcher

# tests
go test ./... -race

# lint
go vet ./...
staticcheck ./...
```

## Контракт что НЕ ломаем

- Файлы в `/tmp/avalant_cache/`: `books.<exchange>.json`, `books.master.json`,
  `books.json` (merged), `funding.json`, `spot_arbitrage.json` — формат
  bytewise-идентичен Python-версии.
- Redis-каналы: `book:subscribe`, `book:unsubscribe` — те же сигнатуры.
- Env-vars: те же AVALANT_* что Python читает (см. CLAUDE.md секция Environment).

Если что-то ломается в этих контрактах — НЕ изменяем без явного перехода
Python web-роли на новый формат.
