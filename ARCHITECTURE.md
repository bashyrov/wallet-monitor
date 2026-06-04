# Avalant — Полная техническая архитектура с примерами кода

> Для тимлида. Всё с реальными примерами кода из исходников.

---

## ОГЛАВЛЕНИЕ

1. [Общая схема системы](#1-общая-схема-системы)
2. [Старт go-fetcher: что запускается и в каком порядке](#2-старт-go-fetcher)
3. [Путь токена от нуля до стакана](#3-путь-токена-от-нуля-до-стакана)
4. [Symbol Manager: кто решает на что подписываться](#4-symbol-manager)
5. [WS Runner: один на биржу, вся логика соединения](#5-ws-runner)
6. [Стакан по биржам: как каждая биржа подписывается](#6-стаканы-по-биржам)
7. [In-memory Cache: хранение стаканов](#7-in-memory-cache)
8. [Запись в файлы (Dumper)](#8-запись-в-файлы)
9. [Redis: зеркало стаканов](#9-redis-зеркало-стаканов)
10. [Funding rates: как собираются ставки](#10-funding-rates)
11. [Arbitrage compute: как считаются спреды](#11-arbitrage-compute)
12. [WS Broadcaster: как данные попадают в браузер](#12-ws-broadcaster)
13. [Python backend: как читает данные из Go](#13-python-backend)
14. [Все тайминги и числа](#14-все-тайминги-и-числа)
15. [Что можно улучшить](#15-что-можно-улучшить)

---

## 1. ОБЩАЯ СХЕМА СИСТЕМЫ

```
                         ┌─────────────────────────────────────────┐
                         │             go-fetcher (Go)              │
                         │                                          │
  Биржи    ──WS──▶       │  OB Runners (26)   Funding Runners (18) │
  (26 WS)               │       │                    │             │
                         │   cache.Store          funding.Store     │
                         │       │                    │             │
                         │   Dumper (100ms)      FDumper (500ms)   │
                         │       │                    │             │
                         │   Redis Writer         Arb Compute       │
                         │       │               200ms/500ms/30s    │
                         └───────┼────────────────────┼─────────────┘
                                 │                    │
                         ┌───────▼────────────────────▼─────────────┐
                         │         /tmp/avalant_cache/               │
                         │  books.okx.json   funding.json           │
                         │  books.json       arbitrage.json          │
                         └───────────────────────────────────────────┘
                                            │
                         ┌──────────────────▼───────────────────────┐
                         │         Python app (FastAPI)              │
                         │  /api/screener/long-short                 │
                         │  /api/screener/orderbook                  │
                         └───────────────────────────────────────────┘
                                            │
                         ┌──────────────────▼───────────────────────┐
                         │    WS Broadcaster (:8090)                 │
                         │  /ws/long-short  /ws/book  /ws/funding   │
                         └──────────────────────────────────────────┘
                                            │
                                        Браузер
```

---

## 2. СТАРТ GO-FETCHER

**Файл:** `go-fetcher/cmd/fetcher/main.go`

Всё стартует последовательно, затем все компоненты работают параллельно через `errgroup`:

```go
func main() {
    // 1. Конфиг из env-переменных
    cfg := config.Load()

    // 2. In-memory хранилище стаканов (thread-safe)
    store := cache.New()

    // 3. Дампер стаканов в файлы — каждые 100ms
    dumper := cache.NewDumper(store, cfg.CacheDir, cfg.FileDumpInterval)
    // cfg.FileDumpInterval = 100ms (AVALANT_FILE_DUMP_INTERVAL)

    // 4. Хранилище funding rates
    fundingStore := funding.NewStore()

    // 5. Дампер funding в файлы — каждые 500ms
    fundingDumper := funding.NewDumper(fundingStore, cfg.CacheDir, 500*time.Millisecond)

    // 6. Symbol Manager — решает на что подписываться
    mgr := symbols.New()

    // 7. Redis subscriber (слушает book:subscribe от Python)
    subscriber, _ := redisbus.NewSubscriber(cfg.RedisURL, mgr)

    // 8. Redis writer (зеркалит стаканы в ob:<ex>:<sym>)
    writer, _ := redisbus.NewWriter(cfg.RedisURL, cfg.RedisWriteThrottle)
    // cfg.RedisWriteThrottle = 50ms (AVALANT_REDIS_WRITE_THROTTLE)

    // 9. Hook: каждое обновление стакана → Redis
    store.SetOnUpdate(func(ex, sym string, bids, asks []ws.Level) {
        go writer.WriteBook(ex, sym, bids, asks) // async, не блокирует recv loop
        bookCh.OnBookUpdate(ex, sym, bids, asks) // push в WS broadcaster
    })

    g, gctx := errgroup.WithContext(ctx)

    // 10. Arb compute движки
    g.Go(func() error { return arb.NewCompute(..., 200*time.Millisecond).Run(gctx) })
    g.Go(func() error { return arb.NewSpotCompute(..., 500*time.Millisecond).Run(gctx) })
    g.Go(func() error { return arb.NewDEXCompute(..., 30*time.Second).Run(gctx) })

    // 11. 18 funding адаптеров
    for _, fa := range []funding.Adapter{
        fbinance.New(), fbybit.New(), fokx.New(), fbitget.New(),
        faster.New(), fgate.New(), fkucoin.New(), fmexc.New(),
        fbingx.New(), fhtx.New(), fhyperliquid.New(), fwhitebit.New(),
        fparadex.New(), fkraken.New(), fbackpack.New(), flighter.New(),
        fethereal.New(), fextended.New(),
    } {
        runner := funding.NewRunner(fa, fundingStore)
        mgr.RegisterFunding(fa.Name(), runner)
        g.Go(func() error { runner.Run(gctx); return nil })
    }

    // 12. Cache pruner — каждые 60s удаляет неактивные символы
    g.Go(func() error {
        t := time.NewTicker(60 * time.Second)
        for range t.C {
            store.Prune(cfg.IdleTimeout) // IdleTimeout = 60s
        }
    })

    // 13. Stale eviction — каждые 60s удаляет устаревшие (>30min)
    g.Go(func() error {
        t := time.NewTicker(60 * time.Second)
        for range t.C {
            store.EvictStale(30 * time.Minute)
        }
    })

    // 14. 26+ orderbook адаптеров (futures + spot по всем биржам)
    for _, e := range orderbookRegistry(cfg, store) {
        mgr.RegisterOrderbook(e.name, e.runner)
        g.Go(func() error { e.runner.Run(gctx); return nil })
    }

    // 15. Symbol Manager reconcile loop (каждые 5s)
    g.Go(func() error { mgr.Run(gctx); return nil })

    // 16. Prewarm — начальный и периодический (каждые 60s)
    startSymbols := bootstrap.TopSymbols(bootstrapDir, cfg.PrewarmTopN)
    mgr.PrewarmAll(startSymbols)
    g.Go(func() error {
        time.Sleep(5 * time.Second) // ждём первый arbitrage.json
        mgr.PrewarmFromArbFiles(cfg.CacheDir, bootstrap.Default20)
        t := time.NewTicker(60 * time.Second)
        for range t.C {
            mgr.PrewarmFromArbFiles(cfg.CacheDir, bootstrap.Default20)
        }
    })

    g.Wait()
}
```

### Полный список горутин (~70-95 на полном проде):

| Горутина | Кол-во | Интервал |
|----------|--------|----------|
| OB Dumper | 1 | 100ms |
| Funding Dumper | 1 | 500ms |
| Arb Compute (futures) | 1 | 200ms |
| Arb Compute (spot) | 1 | 500ms |
| Arb Compute (DEX) | 1 | 30s |
| Symbol Manager reconcile | 1 | 5s |
| Redis subscriber | 1 | long-poll |
| Funding runners | 18 | per-adapter |
| Cache pruner | 1 | 60s |
| Stale eviction | 1 | 60s |
| OB WS runners | 26+ | continuous |
| Trade tick runners | 17 | continuous |
| WS broadcaster | 1 | event-driven |
| Prewarm refresh | 1 | 60s |

---

## 3. ПУТЬ ТОКЕНА ОТ НУЛЯ ДО СТАКАНА

### Шаг 1 — Bootstrap: откуда берутся первые символы

**Файл:** `go-fetcher/internal/bootstrap/symbols.go`

```go
// Default20 — fallback если нет arbitrage.json
var Default20 = []string{
    "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX",
    "LINK", "DOT", "MATIC", "UNI", "LTC", "BCH", "ATOM", "FIL",
    "ETC", "NEAR", "ICP", "APT",
}

// TopSymbols читает funding.json, ранжирует по volume_usd_24h, возвращает топ-N
func TopSymbols(cacheDir string, n int) []string {
    data, err := os.ReadFile(filepath.Join(cacheDir, "funding.json"))
    if err != nil {
        return Default20 // файл не найден → дефолт
    }
    // парсит JSON, берёт max(volume) per symbol across venues
    // возвращает топ-N по убыванию объёма
    ...
}
```

На проде: `AVALANT_PREWARM_TOP_N=1000` → берёт топ-1000 символов по объёму.

### Шаг 2 — PrewarmAll: раздать символы всем адаптерам

```go
// Применяет один список символов ко всем зарегистрированным venue
func (m *Manager) PrewarmAll(syms []string) {
    bucket := make(map[string]struct{}, len(syms))
    for _, s := range syms {
        bucket[s] = struct{}{}
    }
    // Одинаковый список для всех OB runners + funding runners + tick runners
    for venue := range m.obRunners {
        m.prewarm[venue] = copySet(bucket)
    }
}
```

### Шаг 3 — Reconcile: применить символы к WS-подключениям

```go
// Запускается каждые 5 секунд
const IdleWindow = 120 * time.Second

func (m *Manager) reconcile() {
    for venue, runner := range m.obRunners {
        // Объединение prewarm + живые user-touches
        union := copySet(m.prewarm[venue])
        for sym, lastTouch := range m.userSubs[venue] {
            if time.Since(lastTouch) < IdleWindow { // 120s
                union[sym] = struct{}{}
            } else {
                delete(m.userSubs[venue], sym) // устарел — удаляем
            }
        }
        // Если набор изменился — применяем к runner
        if !setsEqual(union, m.current[venue]) {
            syms := setToSlice(union)
            runner.SetSymbols(syms)
            m.current[venue] = union
        }
    }
}
```

### Шаг 4 — SetSymbols: подписка/отписка в runner

```go
func (r *Runner) SetSymbols(syms []string) {
    r.symMu.Lock()
    wanted := toSet(syms)

    // Проверяем есть ли символы на удаление
    hasRemoved := false
    for s := range r.symbols {
        if _, ok := wanted[s]; !ok {
            hasRemoved = true
            break
        }
    }
    added := newSymbols(wanted, r.subscribed) // diff: новые - уже подписанные
    r.symbols = wanted
    conn := r.conn
    r.symMu.Unlock()

    if hasRemoved && conn != nil {
        conn.Close() // форс-реконнект с новым набором
        return
    }
    if conn != nil && len(added) > 0 {
        go r.subscribe(conn, added) // дельта-подписка без реконнекта
    }
}
```

### Шаг 5 — subscribe: отправка фреймов на биржу

```go
func (r *Runner) subscribe(conn *websocket.Conn, syms []string) error {
    // Обрезаем если биржа имеет лимит (KuCoin: 50)
    if max := r.a.MaxSymbols(); max > 0 && len(syms) > max {
        syms = syms[:max]
    }

    // Адаптер строит фреймы (с чанкингом)
    frames := r.a.BuildSubscribe(syms)
    // OKX: 100 символов/фрейм → 10 фреймов для 1000 символов
    // Bitget: 50 символов/фрейм + 200ms задержка → 20 фреймов

    delay := r.a.SubscribeDelay()
    for i, frame := range frames {
        r.safeSend(conn, frame) // через writeMu — сериализует 3 writer'а

        if i == 0 {
            r.subscribedAt.Store(time.Now()) // для zombie watchdog
        }
        if delay > 0 && i < len(frames)-1 {
            time.Sleep(delay) // Bitget: 200ms, KuCoin: 350ms
        }
    }
    return nil
}
```

### Шаг 6 — Recv loop: получение данных

```go
func (r *Runner) session(ctx context.Context) error {
    // ... соединение ...

    // Переопределяем обработчики ping/pong
    conn.SetPingHandler(func(data string) error {
        r.lastMsg.Store(time.Now()) // обновляем "видели активность"
        return prevPing(data)
    })

    go r.staleWatchdog(wdCtx, conn) // запускаем watchdog
    go r.heartbeatLoop(...)         // периодический ping на биржу

    frameCount := 0
    for {
        mt, raw, err := conn.ReadMessage()
        if err != nil { return err }

        r.lastMsg.Store(time.Now()) // любой фрейм = connection alive

        // Для HTX и BingX: распаковываем gzip
        if r.a.DecompressGzip() {
            raw, _ = gunzip(raw) // переиспользует gzip.Reader из sync.Pool
        }

        // Поглощаем ping/pong на уровне текста (Bitget "pong", KuCoin "ping")
        if bytes.Equal(bytes.ToLower(bytes.TrimSpace(raw)), []byte("pong")) {
            continue
        }
        if bytes.Equal(bytes.ToLower(bytes.TrimSpace(raw)), []byte("ping")) {
            r.safeSend(conn, r.a.PongFor(raw))
            continue
        }

        // Парсим фрейм через адаптер
        snap, _ := r.a.Parse(raw)
        if snap == nil { continue } // ack подписки, error event → не данные

        // Реальный data-фрейм
        if frameCount == 0 { r.bo.ResetPolicy() } // сброс policy backoff
        frameCount++
        r.lastData.Store(time.Now()) // для zombie watchdog

        // Обрезаем до 200 уровней с каждой стороны
        if len(snap.Bids) > 200 { snap.Bids = snap.Bids[:200] }
        if len(snap.Asks) > 200 { snap.Asks = snap.Asks[:200] }

        // Передаём в cache.Store → Redis → broadcaster
        r.onUpdate(r.a.Name(), *snap)
    }
}
```

---

## 4. SYMBOL MANAGER

**Файл:** `go-fetcher/internal/symbols/manager.go`

```go
const IdleWindow = 120 * time.Second // = Python's _USER_SUB_TTL_S

type Manager struct {
    mu sync.Mutex
    prewarm  map[string]map[string]struct{} // venue → hot symbols
    userSubs map[string]map[string]time.Time // venue → sym → last touch
    obRunners      map[string]*ws.Runner
    fundingRunners map[string]*funding.Runner
    tickRunners    map[string]*ticks.Runner
    current map[string]map[string]struct{} // last-applied per venue
}

// Run запускается одной горутиной, reconcile каждые 5 секунд
func (m *Manager) Run(ctx context.Context) {
    t := time.NewTicker(5 * time.Second)
    defer t.Stop()
    for {
        select {
        case <-ctx.Done(): return
        case <-t.C: m.reconcile()
        }
    }
}

// Touch вызывается из Redis subscriber когда Python пишет book:subscribe
func (m *Manager) Touch(venue, symbol string) {
    m.mu.Lock()
    defer m.mu.Unlock()
    if m.userSubs[venue] == nil {
        m.userSubs[venue] = make(map[string]time.Time)
    }
    m.userSubs[venue][symbol] = time.Now()
    // reconcile подхватит на следующем тике (≤5s)
}

// PrewarmFromArbFiles — читает arbitrage.json и строит per-venue набор
// Каждый venue получает только те символы, где он фигурирует как long_ex или short_ex
func (m *Manager) PrewarmFromArbFiles(cacheDir string, fallback []string) {
    per := make(map[string]map[string]struct{})

    // Futures arb
    var futDoc struct{ Opps []struct {
        Symbol, LongExchange, ShortExchange string
    } `json:"opportunities"` }
    data, _ := os.ReadFile(filepath.Join(cacheDir, "arbitrage.json"))
    sonic.Unmarshal(data, &futDoc)
    for _, o := range futDoc.Opps {
        per[o.LongExchange][o.Symbol] = struct{}{}
        per[o.ShortExchange][o.Symbol] = struct{}{}
    }

    // Spot arb (binance_spot, gate_spot, etc.)
    var spotDoc struct{ Opps []struct {
        Symbol, SpotExchange, ShortExchange string
    } `json:"opportunities"` }
    data, _ = os.ReadFile(filepath.Join(cacheDir, "spot_arbitrage.json"))
    sonic.Unmarshal(data, &spotDoc)
    for _, o := range spotDoc.Opps {
        per[o.SpotExchange+"_spot"][o.Symbol] = struct{}{}
        per[o.ShortExchange][o.Symbol] = struct{}{}
    }

    // Применяем + добавляем Default20 как fallback
    m.mu.Lock()
    for venue, syms := range per {
        for _, f := range fallback {
            syms[f] = struct{}{}
        }
        m.prewarm[venue] = syms
    }
    m.mu.Unlock()
}
```

---

## 5. WS RUNNER

**Файл:** `go-fetcher/internal/ws/runner.go`

### Структура

```go
type Runner struct {
    a        Adapter       // реализация конкретной биржи
    onUpdate UpdateFunc    // callback → cache.Store

    symMu      sync.Mutex
    symbols    map[string]struct{} // что хотим (от SetSymbols)
    subscribed map[string]struct{} // что уже отправили на биржу
    conn       *websocket.Conn

    writeMu      sync.Mutex        // ВАЖНО: 3 writer'а → 1 mutex
    bo           Backoff
    lastMsg      atomic[time.Time] // любой входящий фрейм
    lastData     atomic[time.Time] // только data-фреймы (snap != nil)
    subscribedAt atomic[time.Time] // когда отправили первый subscribe
    log          *zerolog.Logger
}

// Потокобезопасный generic atomic — нет sync/atomic.Time в Go
type atomic[T any] struct {
    mu sync.Mutex
    v  T
}
```

### Watchdog — защита от зависших соединений

```go
const staleThreshold    = 90 * time.Second // нет вообще никаких фреймов
const dataStaleThreshold = 5 * time.Minute // есть ping/pong, нет данных

func (r *Runner) staleWatchdog(ctx context.Context, conn *websocket.Conn) {
    t := time.NewTicker(5 * time.Second) // проверяем каждые 5s
    for {
        select {
        case <-t.C:
            // Проверка 1: нет вообще никакой активности
            if age := time.Since(r.lastMsg.Load()); age > staleThreshold {
                conn.WriteControl(websocket.CloseMessage, ...)
                conn.Close()
                return // recv loop получит ошибку → backoff → reconnect
            }

            // Проверка 2: zombie-соединение
            // TCP жив (ping/pong ходят), но данных нет
            // OKX и Bitget делали это — тихо дропали подписку
            subAt := r.subscribedAt.Load()
            if time.Since(subAt) < dataStaleThreshold { continue }
            if len(r.subscribed) == 0 { continue }

            noDataFor := time.Since(subAt)
            if ld := r.lastData.Load(); !ld.IsZero() {
                noDataFor = time.Since(ld)
            }
            if noDataFor > dataStaleThreshold {
                // "WS zombie — no data frames, forcing reconnect"
                conn.Close()
                return
            }
        }
    }
}
```

### Backoff логика

```go
// go-fetcher/internal/ws/policy.go

// Policy-close коды — требуют длинного backoff
var policyCloseCodes = []int{
    1008, // Binance: "Pong timeout"
    1011, // Hyperliquid: "keepalive timeout"
    3001, // Aster: policy violation
    4400, 4401, // auth errors
}

type Backoff struct {
    transientCur time.Duration // 300ms → 600ms → 1.2s → ... → 30s
    policyCur    time.Duration // 30s → 60s → 120s → ... → 5min
}

func (b *Backoff) NextTransient() time.Duration {
    cur := b.transientCur
    if cur == 0 { cur = 300 * time.Millisecond }
    b.transientCur = min(cur*2, 30*time.Second)
    return cur
}

func (b *Backoff) NextPolicy() time.Duration {
    cur := b.policyCur
    if cur == 0 { cur = 30 * time.Second }
    b.policyCur = min(cur*2, 5*time.Minute)
    return cur
}

// Сброс только при первом data-фрейме, не при простом connect/ack
func (b *Backoff) ResetPolicy() { b.policyCur = 0 }
func (b *Backoff) ResetTransient() { b.transientCur = 0 }
```

---

## 6. СТАКАНЫ ПО БИРЖАМ

### Binance Futures

**Файл:** `go-fetcher/internal/exchanges/binance/futures.go`

```go
// URL строится динамически — combined stream с несколькими парами
// wss://fstream.binance.com/stream?streams=btcusdt@depth20@100ms/ethusdt@depth20@100ms/...
func (a *Futures) URL(ctx context.Context) (string, error) {
    a.symMu.RLock()
    syms := make([]string, 0, len(a.symbols))
    for s := range a.symbols { syms = append(syms, s) }
    a.symMu.RUnlock()

    streams := make([]string, len(syms))
    for i, s := range syms {
        streams[i] = strings.ToLower(s) + "usdt@depth20@100ms"
    }
    return "wss://fstream.binance.com/stream?streams=" + strings.Join(streams, "/"), nil
}

// Binance: нет SUBSCRIBE команды — символы в URL
func (a *Futures) BuildSubscribe(_ []string) [][]byte { return nil }
func (a *Futures) SubscribeDelay() time.Duration { return 250 * time.Millisecond }
func (a *Futures) MaxSymbols() int { return 200 } // лимит combined stream

// Парсинг: Binance шлёт snapshot (не delta) каждые 100ms
func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
    var msg struct {
        Stream string `json:"stream"` // "btcusdt@depth20@100ms"
        Data   struct {
            Bids [][]string `json:"b"` // [["65000.0","1.234"],...]
            Asks [][]string `json:"a"`
        } `json:"data"`
    }
    sonic.Unmarshal(frame, &msg)
    // Извлекаем токен из stream name
    // "btcusdt@depth20@100ms" → убираем "usdt@..." → "BTC"
    token := strings.ToUpper(strings.Split(msg.Stream, "usdt@")[0])
    return &ws.Snapshot{
        Symbol: token,
        Bids:   parseLevels(msg.Data.Bids),
        Asks:   parseLevels(msg.Data.Asks),
    }, nil
}
```

### OKX Futures

**Файл:** `go-fetcher/internal/exchanges/okx/futures.go`

```go
const futuresWS = "wss://ws.okx.com:8443/ws/v5/public"

// OKX использует subscribe команду, 100 символов/фрейм
func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
    const chunkSize = 100
    frames := make([][]byte, 0, (len(symbols)+chunkSize-1)/chunkSize)
    for i := 0; i < len(symbols); i += chunkSize {
        end := min(i+chunkSize, len(symbols))
        args := make([]map[string]string, end-i)
        for j, s := range symbols[i:end] {
            args[j] = map[string]string{
                "channel": "books",         // публичный канал (не books50-l2-tbt — тот требует auth!)
                "instId":  strings.ToUpper(s) + "-USDT-SWAP",
            }
        }
        b, _ := ws.MarshalJSON(map[string]any{"op": "subscribe", "args": args})
        frames = append(frames, b)
    }
    return frames
}

// OKX: snapshot + delta (incremental update)
func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
    var msg struct {
        Event  string `json:"event"` // "subscribe", "error"
        Arg    struct { Channel string; InstID string } `json:"arg"`
        Action string `json:"action"` // "snapshot" | "update"
        Data   []struct {
            Bids [][]string `json:"bids"` // [[px, sz, "0", orders], ...]
            Asks [][]string `json:"asks"`
        } `json:"data"`
    }
    sonic.Unmarshal(frame, &msg)

    if msg.Event != "" { return nil, nil } // ack / error — не данные
    if msg.Arg.Channel != "books" { return nil, nil }

    token := strings.TrimSuffix(msg.Arg.InstID, a.instSuffix)
    bk := a.getOrCreate(token)

    if msg.Action == "snapshot" {
        bk.bids = make(map[float64]float64) // полная замена
        bk.asks = make(map[float64]float64)
    }
    // Merge delta: sz=0 → удалить уровень, sz>0 → обновить
    apply(bk.bids, msg.Data[0].Bids)
    apply(bk.asks, msg.Data[0].Asks)

    return &ws.Snapshot{
        Symbol: token,
        Bids:   ws.SortedLevels(bk.bids, ws.Bids, 200),
        Asks:   ws.SortedLevels(bk.asks, ws.Asks, 200),
    }, nil
}

// App-level heartbeat (не WS ping frame)
func (a *Futures) Heartbeat() []byte                { return []byte("ping") }
func (a *Futures) HeartbeatInterval() time.Duration { return 25 * time.Second }
func (a *Futures) UseLibPings() bool                { return false }
```

### Bitget Futures

**Файл:** `go-fetcher/internal/exchanges/bitget/futures.go`

```go
const baseURL = "wss://ws.bitget.com/v2/ws/public"

// 50 символов/фрейм + 200ms задержка (200 → error 30002 "Unrecognized request")
func (a *Adapter) BuildSubscribe(symbols []string) [][]byte {
    const chunkSize = 50
    for i := 0; i < len(symbols); i += chunkSize {
        args[j] = map[string]string{
            "instType": a.instType, // "USDT-FUTURES" или "SPOT"
            "channel":  "books15",  // 15 уровней, ~100-200ms
            "instId":   strings.ToUpper(s) + "USDT",
        }
    }
}

func (a *Adapter) SubscribeDelay() time.Duration { return 200 * time.Millisecond }
func (a *Adapter) Heartbeat() []byte             { return []byte("ping") }
func (a *Adapter) HeartbeatInterval() time.Duration { return 25 * time.Second }
func (a *Adapter) UseLibPings() bool             { return false } // Bug #6
```

### HTX (gzip + delta)

**Файл:** `go-fetcher/internal/exchanges/htx/futures.go`

```go
// HTX шлёт gzip-сжатые фреймы
func (a *Futures) DecompressGzip() bool { return true }

// Отдельный фрейм на каждый символ
func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
    frames := make([][]byte, len(symbols))
    for i, s := range symbols {
        sub := map[string]any{
            "sub": "market." + strings.ToUpper(s) + "-USDT.depth.size_20.high_freq",
            "id":  "sub-" + s,
        }
        frames[i], _ = ws.MarshalJSON(sub)
    }
    return frames
}

// HTX ping/pong — JSON формат, не текст
// Server → {"op":"ping","ts":1234567890}
// Client → {"op":"pong","ts":1234567890}
func (a *Futures) PongFor(raw []byte) []byte {
    var msg struct { OP string `json:"op"`; TS int64 `json:"ts"` }
    if sonic.Unmarshal(raw, &msg) == nil && msg.OP == "ping" {
        resp, _ := sonic.Marshal(map[string]any{"op": "pong", "ts": msg.TS})
        return resp
    }
    return nil
}
```

### KuCoin (требует токен через REST)

**Файл:** `go-fetcher/internal/exchanges/kucoin/futures.go`

```go
// KuCoin: перед подключением нужен токен через REST
func (a *Futures) URL(ctx context.Context) (string, error) {
    // POST https://api-futures.kucoin.com/api/v1/bullet-public
    // Получаем token + endpoint
    token, endpoint, err := a.fetchToken(ctx)
    return endpoint + "?token=" + token + "&connectId=avlf-" + ..., nil
}

// Лимит: 3 msg/sec → 1 символ/фрейм + 350ms задержка
func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
    frames := make([][]byte, len(symbols))
    for i, s := range symbols {
        frames[i], _ = ws.MarshalJSON(map[string]any{
            "type":     "subscribe",
            "topic":    "/contractMarket/level2Depth50:" + strings.ToUpper(s) + "USDTM",
            "response": true,
        })
    }
    return frames
}

func (a *Futures) SubscribeDelay() time.Duration { return 350 * time.Millisecond }
func (a *Futures) MaxSymbols() int               { return 50 } // hard cap
func (a *Futures) HeartbeatInterval() time.Duration { return 15 * time.Second }
```

### Bybit (delta + seq tracking)

**Файл:** `go-fetcher/internal/exchanges/bybit/futures.go`

```go
func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
    topics := make([]string, len(symbols))
    for i, s := range symbols {
        topics[i] = "orderbook.50." + strings.ToUpper(s) + "USDT"
    }
    b, _ := ws.MarshalJSON(map[string]any{
        "op":   "subscribe",
        "args": topics,
    })
    return [][]byte{b}
}

// Bybit: snapshot затем delta
func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
    var msg struct {
        Topic string `json:"topic"` // "orderbook.50.BTCUSDT"
        Type  string `json:"type"` // "snapshot" | "delta"
        Data  struct {
            Bids [][]string `json:"b"`
            Asks [][]string `json:"a"`
            Seq  uint64     `json:"seq"` // sequence number
        } `json:"data"`
    }
    // merge delta в book[token]
    // sz="0" → delete level, else update
}

func (a *Futures) HeartbeatInterval() time.Duration { return 20 * time.Second }
func (a *Futures) Heartbeat() []byte { return []byte(`{"op":"ping"}`) }
```

### BingX (gzip + text ping)

```go
func (a *Futures) DecompressGzip() bool { return true }
func (a *Futures) Heartbeat() []byte    { return []byte("Ping") }
// Server шлёт "Ping", клиент отвечает "Pong"
func (a *Futures) PongFor(raw []byte) []byte {
    if bytes.Equal(bytes.TrimSpace(raw), []byte("Ping")) { return []byte("Pong") }
    return nil
}
func (a *Futures) UseLibPings() bool { return false }

func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
    // BingX: один фрейм для всех
    args := make([]map[string]string, len(symbols))
    for i, s := range symbols {
        args[i] = map[string]string{
            "id":    "sub-" + s,
            "reqType": "sub",
            "dataType": strings.ToUpper(s) + "-USDT@depth20",
        }
    }
}
func (a *Futures) MaxSymbols() int { return 100 }
```

---

## 7. IN-MEMORY CACHE

**Файл:** `go-fetcher/internal/cache/store.go`

```go
type Store struct {
    mu       sync.RWMutex
    books    map[string]*Entry  // ключ: "exchange:symbol"
    versions map[string]uint64  // версия per-venue (для Dumper'а)
    onUpdate func(ex, sym string, bids, asks []ws.Level)
}

type Entry struct {
    Bids          []ws.Level  // [[price, size], ...]
    Asks          []ws.Level
    UpdatedAt     time.Time   // когда обновлён
    LastRequestAt time.Time   // когда последний раз запрашивали (для prune)
    Source        string      // "ws" или "rest"
}

// Store — вызывается из onUpdate callback в runner
func (s *Store) Store(exchange, symbol string, snap ws.Snapshot, source string) {
    key := exchange + ":" + symbol
    s.mu.Lock()
    s.books[key] = &Entry{
        Bids:      snap.Bids,
        Asks:      snap.Asks,
        UpdatedAt: time.Now(),
        Source:    source,
    }
    s.versions[exchange]++ // Dumper проверяет: если не изменилось → skip
    s.mu.Unlock()

    if s.onUpdate != nil {
        s.onUpdate(exchange, symbol, snap.Bids, snap.Asks)
        // ↑ это вызывает: Redis write + book broadcaster
    }
}

// Prune — убирает символы без активности дольше idle
func (s *Store) Prune(idle time.Duration) int {
    s.mu.Lock()
    defer s.mu.Unlock()
    removed := 0
    for key, entry := range s.books {
        if time.Since(entry.LastRequestAt) > idle {
            delete(s.books, key)
            removed++
        }
    }
    return removed
}

// EvictStale — убирает символы без обновлений дольше staleAfter (30min)
// Нужно для делистнутых символов — биржа держит подписку, но данные не шлёт
func (s *Store) EvictStale(staleAfter time.Duration) int {
    // аналогично Prune, но по UpdatedAt
}
```

---

## 8. ЗАПИСЬ В ФАЙЛЫ

**Файл:** `go-fetcher/internal/cache/files.go`

```go
type Dumper struct {
    store        *Store
    cacheDir     string
    interval     time.Duration    // 100ms
    lastVersions map[string]uint64 // per-venue версия при последнем дампе
}

func (d *Dumper) Run(ctx context.Context) error {
    t := time.NewTicker(d.interval) // каждые 100ms
    for {
        select {
        case <-t.C: d.tick()
        case <-ctx.Done(): return nil
        }
    }
}

func (d *Dumper) tick() {
    snap := d.store.Snapshot() // быстрый RLock + shallow copy
    now := time.Now()

    for exchange, entries := range snap {
        // Оптимизация: если версия не изменилась → пропускаем
        curVer := d.store.VersionFor(exchange)
        if d.lastVersions[exchange] == curVer && curVer != 0 {
            continue // нет новых данных → не перезаписываем файл
        }
        d.lastVersions[exchange] = curVer

        // Сериализуем и пишем books.<exchange>.json
        body, _ := sonic.Marshal(entries)
        atomicWrite(filepath.Join(d.cacheDir, "books."+exchange+".json"), body)
    }

    // books.json (общий мёрж) — не чаще раза в 1 секунду
    if time.Since(d.lastFullMerge) > time.Second {
        allEntries := mergeAll(snap)
        body, _ := sonic.Marshal(allEntries)
        atomicWrite(filepath.Join(d.cacheDir, "books.json"), body)
        d.lastFullMerge = now
    }
}

// Атомарная запись: tempfile → rename (POSIX гарантирует атомарность)
func atomicWrite(path string, data []byte) error {
    dir := filepath.Dir(path)
    tmp, _ := os.CreateTemp(dir, "."+filepath.Base(path)+".tmp.")
    tmp.Write(data)
    tmp.Close()
    return os.Rename(tmp.Name(), path) // атомарно на Linux, os.Replace на Windows
}
```

**Формат файла:**
```json
{
    "okx:BTC": {
        "bids": [[65000.0, 1.234], [64999.5, 0.891]],
        "asks": [[65000.5, 0.567], [65001.0, 2.100]],
        "ts": 1780579560.123456,
        "last_request": 1780579558.000000,
        "source": "ws"
    },
    "okx:ETH": { ... }
}
```

---

## 9. REDIS ЗЕРКАЛО

**Файл:** `go-fetcher/internal/redisbus/writer.go`

```go
const obTTL = 10 * time.Second // Python читает с таким же ожиданием

type Writer struct {
    client    *redis.Client
    throttle  time.Duration        // 50ms по умолчанию
    mu        sync.Mutex
    lastWrite map[string]time.Time // per-key rate limiter
}

func (w *Writer) WriteBook(exchange, symbol string, bids, asks []ws.Level) {
    key := "ob:" + exchange + ":" + symbol

    // Rate limiting: не чаще 50ms на ключ
    w.mu.Lock()
    if now.Sub(w.lastWrite[key]) < w.throttle {
        w.mu.Unlock()
        return // пропускаем — недавно писали
    }
    w.lastWrite[key] = time.Now()
    w.mu.Unlock()

    // Формат совместим с Python's orderbook_redis.py
    payload := map[string]any{
        "ts": float64(time.Now().UnixMilli()) / 1000.0, // epoch seconds float
        "data": map[string]any{
            "bids": bids, // [[px,sz], ...]
            "asks": asks,
        },
    }
    body, _ := sonic.Marshal(payload)

    // 1s timeout — если Redis тормозит, лучше пропустить, чем блокировать recv loop
    ctx, cancel := context.WithTimeout(context.Background(), 1*time.Second)
    defer cancel()
    w.client.Set(ctx, key, body, obTTL) // SETEX ob:okx:BTC → TTL 10s
}
```

**Redis subscriber** — слушает команды от Python:

```go
// go-fetcher/internal/redisbus/subscriber.go

func (s *Subscriber) Run(ctx context.Context) {
    pubsub := s.client.Subscribe(ctx, "book:subscribe", "book:unsubscribe")
    for msg := range pubsub.Channel() {
        // Payload: "okx:BTC"
        parts := strings.SplitN(msg.Payload, ":", 2)
        venue, symbol := parts[0], parts[1]

        switch msg.Channel {
        case "book:subscribe":
            s.mgr.Touch(venue, symbol)   // продлеваем TTL 120s
        case "book:unsubscribe":
            s.mgr.Untouch(venue, symbol) // немедленно убираем из user-touches
        }
    }
}
```

---

## 10. FUNDING RATES

**Файл:** `go-fetcher/internal/funding/runner.go`

```go
type Runner struct {
    adapter  Adapter
    store    *Store
}

func (r *Runner) Run(ctx context.Context) {
    // Два параллельных пути:
    g.Go(func() { r.wsLoop(ctx) })    // WS — primary (sub-second)
    g.Go(func() { r.restLoop(ctx) })  // REST backstop — fallback
}

func (r *Runner) wsLoop(ctx context.Context) {
    // Аналогично orderbook runner — connect, subscribe, recv loop
    // Та же backoff логика: transient 300ms→30s, policy 30s→5min
}

func (r *Runner) restLoop(ctx context.Context) {
    interval := r.adapter.BackstopInterval() // 2s для WS-бирж, 5min для DEX
    t := time.NewTicker(interval)
    for range t.C {
        ticks, _ := r.adapter.BackstopFetch(ctx, symbols)
        for _, tick := range ticks {
            r.store.Store(r.adapter.Name(), tick)
        }
    }
}
```

### Binance funding (WS)

**Файл:** `go-fetcher/internal/funding/binance/binance.go`

```go
const wsURL = "wss://fstream.binance.com/stream?streams=!markPrice@arr@1s/!ticker@arr"
// Два стрима в одном соединении:
// !markPrice@arr@1s — все mark prices каждую секунду
// !ticker@arr — 24h ticker (нужен для объёма)

func (a *Adapter) ParseWS(raw []byte) ([]funding.Tick, error) {
    var msg struct {
        Stream string `json:"stream"`
        Data   json.RawMessage `json:"data"`
    }
    sonic.Unmarshal(raw, &msg)

    switch {
    case strings.HasPrefix(msg.Stream, "!markPrice"):
        // [{"e":"markPriceUpdate","s":"BTCUSDT","p":"65000","r":"0.0001","T":1234567890000}]
        var rows []struct {
            Symbol      string `json:"s"` // "BTCUSDT"
            MarkPrice   string `json:"p"`
            FundingRate string `json:"r"`
            NextFunding int64  `json:"T"` // unix ms
        }
        sonic.Unmarshal(msg.Data, &rows)
        // rate * 3 = 8h annualized

    case strings.HasPrefix(msg.Stream, "!ticker"):
        // объём из ticker (volume в BTC → нужно умножить на цену)
    }
}

// Кеш торгуемых символов — 10 минут, чтобы не показывать делистнутые
// (Binance оставляет NTRN в !markPrice@arr несмотря на делистинг)
var tradingSetCache struct {
    ttl  time.Time
    data map[string]bool
    mu   sync.Mutex
}
const tradingSetTTL = 10 * time.Minute
```

### Paradex (REST-only, StarkNet DEX)

**Файл:** `go-fetcher/internal/funding/paradex/paradex.go`

```go
const restURL = "https://api.prod.paradex.trade/v1/markets/summary?market=ALL"

func (a *Adapter) BackstopFetch(ctx context.Context, _ []string) ([]funding.Tick, error) {
    body, _ := getJSON(ctx, restURL)
    var doc struct {
        Results []struct {
            Symbol      string      `json:"symbol"`      // "BTC-USD-PERP"
            MarkPrice   interface{} `json:"mark_price"`  // строка!
            FundingRate interface{} `json:"funding_rate"` // строка!
            Volume24h   interface{} `json:"volume_24h"`  // строка!
        } `json:"results"`
    }
    sonic.Unmarshal(body, &doc)

    now := time.Now().Unix()
    intervalS := int64(8 * 3600) // 8-часовой интервал
    nextFunding := time.Unix((now/intervalS+1)*intervalS, 0)

    for _, r := range doc.Results {
        if !strings.HasSuffix(r.Symbol, "-USD-PERP") { continue }
        base := strings.TrimSuffix(r.Symbol, "-USD-PERP")
        ticks = append(ticks, funding.Tick{
            Symbol:      base,
            Rate:        funding.ParseFloat(r.FundingRate),
            MarkPrice:   funding.ParseFloat(r.MarkPrice),
            Volume24h:   funding.ParseFloat(r.Volume24h), // уже в USD
            NextFunding: nextFunding,
            IntervalH:   8.0,
        })
    }
}

func (a *Adapter) BackstopInterval() time.Duration { return 5 * time.Minute }
```

---

## 11. ARBITRAGE COMPUTE

**Файл:** `go-fetcher/internal/arb/futures.go`

```go
// Комиссии (taker roundtrip)
var exchangeFees = map[string]float64{
    "binance": 0.0004, "bybit": 0.00055, "okx": 0.0005,
    "gate": 0.0005, "kucoin": 0.0006, "mexc": 0.0002,
    "bitget": 0.0006, "hyperliquid": 0.00035, "aster": 0.0005,
    "paradex": 0.0003, "lighter": 0.0003, "ethereal": 0.0003,
    // default: 0.0006
}

// Гистерезис
const (
    oppMinLifetime = 1 * time.Second  // новая оппортунить появляется через 1s
    oppPurgeAfter  = 90 * time.Second // удаляется если нет данных 90s
)

func (c *Compute) tick() {
    snap := c.store.SnapshotAll() // RLock, shallow copy всех funding ticks

    type opp struct {
        Symbol       string
        LongEx       string
        ShortEx      string
        LongRate     float64
        ShortRate    float64
        Basis        float64 // spread_pct после вычета комиссий
        Volume       float64
    }

    var opps []opp
    for sym, byVenue := range snap {
        for longEx, longTick := range byVenue {
            for shortEx, shortTick := range byVenue {
                if longEx == shortEx { continue }
                if longTick.MarkPrice <= 0 || shortTick.MarkPrice <= 0 { continue }

                // Спред = short_rate - long_rate (мы платим long_rate, получаем short_rate)
                rawSpread := shortTick.FundingRate - longTick.FundingRate

                // Вычитаем комиссии (roundtrip: open + close обеих сторон)
                fees := feeOf(longEx) + feeOf(shortEx)
                netSpread := rawSpread - fees*2

                // Фильтр ticker collision
                priceDev := math.Abs(shortTick.MarkPrice-longTick.MarkPrice) / longTick.MarkPrice
                if priceDev > 1.0 { continue } // >100% расхождение → разные токены

                opps = append(opps, opp{
                    Symbol: sym, LongEx: longEx, ShortEx: shortEx,
                    Basis: netSpread,
                    Volume: math.Max(longTick.Volume24h, shortTick.Volume24h),
                })
            }
        }
    }

    // Сортируем по |Basis| descending
    sort.Slice(opps, func(i, j int) bool {
        return math.Abs(opps[i].Basis) > math.Abs(opps[j].Basis)
    })

    // Гистерезис: убираем новые (<1s) и восстанавливаем старые (<90s)
    now := time.Now()
    filtered := opps[:0]
    for _, o := range opps {
        key := oppKey{o.Symbol, o.LongEx, o.ShortEx}
        if fs, ok := c.firstSeen[key]; !ok {
            c.firstSeen[key] = now // первый раз видим
        } else if now.Sub(fs) >= oppMinLifetime {
            c.lastSeen[key] = now
            filtered = append(filtered, o)
        }
    }

    // Удаляем oppurtunities которых не было >90s
    for key, ls := range c.lastSeen {
        if now.Sub(ls) > oppPurgeAfter {
            delete(c.firstSeen, key)
            delete(c.lastSeen, key)
        }
    }

    // Топ-1000 в файл
    if len(filtered) > arbFileTopN { filtered = filtered[:arbFileTopN] }
    body, _ := sonic.Marshal(map[string]any{"opportunities": filtered})
    atomicWrite(filepath.Join(c.cacheDir, "arbitrage.json"), body)
}
```

### Spot Arb

```go
// go-fetcher/internal/arb/spot.go

var spotFees = map[string]float64{
    "binance": 0.001, "bybit": 0.001, "okx": 0.001,
    "gate": 0.001, "kucoin": 0.001, "mexc": 0.0005,
    "bitget": 0.001, "bingx": 0.001, "htx": 0.002,
}

func (c *SpotCompute) tick(ctx context.Context) {
    // 9 спотовых бирж опрашиваются параллельно
    venues := []string{"binance", "bybit", "okx", "gate", "kucoin",
                        "mexc", "bitget", "bingx", "htx"}
    results := make(map[string][]spotTicker)

    var wg sync.WaitGroup
    for _, venue := range venues {
        wg.Add(1)
        go func(v string) {
            defer wg.Done()
            ctx8s, cancel := context.WithTimeout(ctx, 8*time.Second)
            defer cancel()
            tickers, _ := fetchSpotTickers(ctx8s, v) // REST API
            results[v] = tickers
        }(venue)
    }
    wg.Wait()

    // Join: спот цена + funding rate с futures
    for _, spotVenue := range venues {
        for _, ticker := range results[spotVenue] {
            symbol := ticker.Symbol
            fundingTick, ok := c.store.Get(/* futures venue */, symbol)
            if !ok { continue }

            // Basis = (spot_price - perp_mark_price) / mark_price * 100%
            basis := (ticker.Price - fundingTick.MarkPrice) / fundingTick.MarkPrice

            // Чистый basis после комиссий
            netBasis := basis - spotFees[spotVenue] - feeOf(/* futures venue */)

            // Фильтр: |basis| > 5% → скорее всего ticker collision
            if math.Abs(basis) > 0.05 { continue }
        }
    }
}
```

---

## 12. WS BROADCASTER

**Файл:** `go-fetcher/internal/wsbroadcast/service.go`

```go
// Маршруты на порту 8090
func (s *Service) Routes(mux *http.ServeMux) {
    mux.HandleFunc("/api/screener/ws/long-short", s.handleLongShort)
    mux.HandleFunc("/api/screener/ws/arb", s.handleLongShort)      // alias
    mux.HandleFunc("/api/screener/ws/funding", s.handleFunding)
    mux.HandleFunc("/api/screener/ws/book", s.handleBook)
    mux.HandleFunc("/api/screener/ws/trades", s.handleTrades)
}

// Все WS endpoint'ы: первый фрейм должен быть {"auth":"<JWT>"} за 5 секунд
func (s *Service) handleLongShort(w http.ResponseWriter, r *http.Request) {
    conn, _ := upgrader.Upgrade(w, r, nil)
    defer conn.Close()

    // Auth timeout 5s
    conn.SetReadDeadline(time.Now().Add(5 * time.Second))
    _, raw, _ := conn.ReadMessage()
    // parse {"auth": "<jwt>"}
    // если невалидный → close(4401)
    conn.SetReadDeadline(time.Time{}) // снимаем deadline

    // Подписываем на hub, hub шлёт diff каждые 100ms
    client := s.longShort.Subscribe()
    defer s.longShort.Unsubscribe(client)
    for msg := range client.Out {
        conn.WriteMessage(websocket.TextMessage, msg)
    }
}
```

### LongShort hub

```go
// go-fetcher/internal/wsbroadcast/longshort.go

type LongShort struct {
    cacheDir    string
    hub         *Hub
    lastMtime   time.Time   // для mtime-skip оптимизации (branch perf/longshort-mtime-skip)
    lastSnap    []byte
}

func (ls *LongShort) Run(ctx context.Context) {
    t := time.NewTicker(100 * time.Millisecond) // 10 раз в секунду
    for range t.C {
        // mtime-skip: если arbitrage.json не изменился → не декодируем
        info, _ := os.Stat(filepath.Join(ls.cacheDir, "arbitrage.json"))
        if info.ModTime() == ls.lastMtime {
            continue // файл не менялся с последнего тика
        }
        ls.lastMtime = info.ModTime()

        newSnap, _ := os.ReadFile(...)
        diff := computeDiff(ls.lastSnap, newSnap) // только изменённые строки
        if diff != nil {
            ls.hub.Broadcast(diff) // всем подключённым клиентам
        }
        ls.lastSnap = newSnap
    }
}
```

### Book hub (стаканы в реальном времени)

```go
// go-fetcher/internal/wsbroadcast/book.go

type Book struct {
    reader  *redisbus.Reader // читает из Redis
    store   *cache.Store     // fallback
    mgr     *symbols.Manager
    pending map[string][]ws.Level // накапливаем обновления между flush
    mu      sync.Mutex
}

// OnBookUpdate — вызывается из store.SetOnUpdate при каждом обновлении стакана
func (b *Book) OnBookUpdate(exchange, symbol string, bids, asks []ws.Level) {
    b.mu.Lock()
    b.pending[exchange+":"+symbol] = struct { bids, asks []ws.Level }{bids, asks}
    b.mu.Unlock()
}

// flush — каждые 200ms отправляем накопленные обновления клиентам
func (b *Book) flushLoop(ctx context.Context) {
    t := time.NewTicker(200 * time.Millisecond)
    for range t.C {
        b.mu.Lock()
        batch := b.pending
        b.pending = make(map[string][]ws.Level)
        b.mu.Unlock()

        for key, update := range batch {
            // отправляем только клиентам подписанным на эту пару
            b.hub.BroadcastToSubs(key, update)
        }
    }
}
```

---

## 13. PYTHON BACKEND

**Файл:** `backend/services/arbitrage_service.py`

```python
CACHE_TTL = 6.0           # кешируем funding view на 6 секунд
_FUNDING_VIEW_TTL = 1.0   # pre-serialised payload cache 1 секунда
IVL_TTL = 3600.0          # interval_h кеш 1 час

def get_long_short():
    """Читает arbitrage.json написанный Go каждые 200ms"""
    path = "/tmp/avalant_cache/arbitrage.json"
    with open(path) as f:
        data = json.load(f)
    return data["opportunities"]

# Warmer loop — держит кеш горячим
async def warmer():
    while True:
        await asyncio.sleep(0.8)  # обновляем каждые 800ms
        _refresh_funding_view()   # pre-serialize для /api/screener/funding
```

**Файл:** `backend/api/v1/screener.py`

```python
_OB_TTL = 0.5     # orderbook кеш 500ms
_PH_TTL = 30.0    # price-history 30s
_OI_TTL = 60.0    # open-interest 60s

async def get_orderbook(symbol, long_ex, short_ex):
    """Цепочка фолбэков для получения стакана"""
    # 1. Redis — самый свежий (TTL 10s, latency ~1ms)
    redis_data = await redis.get(f"ob:{long_ex}:{symbol}")
    if redis_data and age < 0.5:  # не старше 500ms
        return redis_data

    # 2. Per-venue файл — до 100ms stale
    file_data = read_json(f"/tmp/avalant_cache/books.{long_ex}.json")
    if file_data and age < 1.0:
        return file_data[f"{long_ex}:{symbol}"]

    # 3. books.json (общий мёрж) — до 1s stale
    merged = read_json("/tmp/avalant_cache/books.json")
    if merged:
        return merged[f"{long_ex}:{symbol}"]

    # 4. REST запрос к бирже (orderbook_cache.py)
    return await orderbook_cache.fetch(long_ex, symbol)
```

**Файл:** `backend/services/orderbook_cache.py`

```python
POLL_INTERVAL = 0.50    # поллинг каждые 500ms
IDLE_TIMEOUT = 30.0     # стоп если нет запросов 30s
FIRST_WAIT = 0.7        # ждём первый результат 700ms
STALE_FALLBACK = 10.0   # используем кеш если младше 10s

# HTTP клиент с таймаутами
httpx_client = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=10.0, read=4.0, write=3.0, pool=1.0),
    limits=httpx.Limits(max_connections=30, max_keepalive_connections=15),
)
```

---

## 14. ВСЕ ТАЙМИНГИ И ЧИСЛА

### Интервалы обновлений

| Компонент | Интервал | Env переменная |
|-----------|----------|----------------|
| OB файл-дамп | **100ms** | `AVALANT_FILE_DUMP_INTERVAL` |
| Funding дамп | **500ms** | hardcoded |
| Arb compute (futures) | **200ms** | hardcoded |
| Arb compute (spot) | **500ms** | hardcoded |
| Arb compute (DEX) | **30s** | hardcoded |
| Symbol reconcile | **5s** | hardcoded |
| Prewarm refresh | **60s** | hardcoded |
| Cache pruner | **60s** | hardcoded |
| Stale eviction | **60s** | hardcoded |
| WS broadcaster | **100ms** | hardcoded |
| Book flush | **200ms** | hardcoded |
| Python warmer | **800ms** | hardcoded |

### Таймауты соединений

| Параметр | Значение | Где |
|----------|----------|-----|
| WS handshake timeout | **30s** | runner.go:195 |
| Stale watchdog порог | **90s** | runner.go:43 |
| Zombie detector порог | **5 минут** | runner.go:94 |
| Watchdog тик | **5s** | runner.go:444 |
| Auth timeout (broadcaster) | **5s** | wsbroadcast |
| Redis write timeout | **1s** | redisbus/writer.go |
| REST backstop timeout | **8s** | arb/spot.go |
| httpx connect | **10s** | orderbook_cache.py |
| httpx read | **4s** | orderbook_cache.py |

### Backoff таймеры

| Тип | Старт | Множитель | Кап |
|-----|-------|-----------|-----|
| Transient (сеть) | 300ms | ×2 | 30s |
| Policy (1008/1011) | 30s | ×2 | 5 минут |

### Чанкинг подписок

| Биржа | Символов/фрейм | Задержка | MaxSymbols |
|-------|---------------|----------|------------|
| Binance | URL (нет frames) | 250ms reconnect | 200 |
| OKX | 100 | 0ms | неограничен |
| Bitget | 50 | 200ms | неограничен |
| KuCoin | 1 | 350ms | 50 |
| Hyperliquid | all | 500ms | неограничен |
| Bybit/Gate/MEXC | all | 0ms | неограничен |
| BingX | all | 0ms | 100 |

### Размеры данных (prod)

| Файл | Размер | Частота записи |
|------|--------|----------------|
| `books.<ex>.json` | 20-100 KB | 100ms (если данные изменились) |
| `books.json` (мёрж) | ~600 KB | 1s |
| `arbitrage.json` | ~2-5 MB (1000 опп) | 200ms |
| `spot_arbitrage.json` | ~200-500 KB | 500ms |
| `dex_arbitrage.json` | ~50-200 KB | 30s |
| Redis ключ `ob:<ex>:<sym>` | ~2-5 KB | 50ms throttle |

### TTL в Redis

| Ключ | TTL |
|------|-----|
| `ob:<ex>:<sym>` | **10s** |
| Throttle per-key | **50ms** |

### User-touch lifecycle

```
Юзер открывает /arb?p=BTC&l=binance&s=okx
    ↓
Python пишет в Redis: PUBLISH book:subscribe "binance:BTC"
                                              PUBLISH book:subscribe "okx:BTC"
    ↓ (async)
go-fetcher subscriber получает → mgr.Touch("binance", "BTC"), mgr.Touch("okx", "BTC")
    ↓ (≤5 секунд)
Symbol Manager reconcile → если BTC не в prewarm → добавляет в subscribed set
                                                  → runner.SetSymbols(newSet)
    ↓ (время подписки)
Runner отправляет subscribe frame → биржа подтверждает → данные начинают идти
    ↓ (через 120 секунд после последнего touch)
Touch устаревает → следующий reconcile убирает BTC из user-touches
                → если BTC не в prewarm → runner.SetSymbols без BTC → реконнект
```

---

## 15. ЧТО МОЖНО УЛУЧШИТЬ

### Критические

| Проблема | Где | Текущее | Лучше |
|----------|-----|---------|-------|
| `books.json` мёрж раз в 1s | cache/files.go | 1s stale | 200ms или убрать (всё равно Redis) |
| Python читает файлы, не gRPC | архитектура | +100-500ms latency | gRPC stream или shared memory |
| Binance Singapore block | нет прокси | Binance недоступен | 1 прокси EU/US |
| Spot arb REST timeout 8s | arb/spot.go | блокирует если биржа тормозит | circuit breaker + cached fallback |

### Производительность

| Проблема | Где | Эффект |
|----------|-----|--------|
| Redis throttle 50ms | redisbus/writer.go | быстрые апдейты (20ms) теряются |
| arbitrage.json 2-5MB | arb/futures.go | JSON marshal 5-10ms каждые 200ms |
| 26 WS runner'а × goroutine | runner.go | 100+ горутин только на WS recv |
| KuCoin 1 sym/frame + 350ms | kucoin/futures.go | 1000 символов = 350s подписки |
| Python cache TTL 6s | arbitrage_service.py | arb данные могут быть 6s старыми |

### Отсутствующие фичи

| Фича | Статус | Ветка |
|------|--------|-------|
| BBO sub-channels (10ms top-of-book) | в ветках | feat/bybit-bbo-add, feat/okx-bbo-add |
| Seq gap tracking | в ветке | perf/orderbook-seq-tracking |
| mtime-skip для арб.json | в ветке | perf/longshort-mtime-skip (−80% decode) |
| Resync on seq gap | нет | нужно добавить в runner |
| Prometheus метрики | частично | feat/pipeline-metrics |
| Split connections (KuCoin >50 sym) | нет | нужна архитектурная правка |
