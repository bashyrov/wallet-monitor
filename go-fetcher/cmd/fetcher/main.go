// Command fetcher — Go drop-in replacement for the Python orderbook
// fetcher's hot path. Boot order:
//
//  1. Load env-var config
//  2. Set up structured logging
//  3. Create cache.Store
//  4. Spawn one ws.Runner per registered adapter
//  5. Spawn cache.Dumper to flush books to disk every 100ms
//  6. Wait for SIGINT/SIGTERM, then graceful shutdown (cancel ctx, wait
//     for runners to flush their final state)
//
// At Phase 0 the registry is empty — adapters will be added in Phase 1.
package main

import (
	"context"
	"os"
	"os/signal"
	"syscall"
	"time"

	"golang.org/x/sync/errgroup"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/bootstrap"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/config"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/exchanges/aster"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/exchanges/backpack"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/exchanges/binance"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/exchanges/bingx"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/exchanges/bitget"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/exchanges/bybit"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/exchanges/gate"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/exchanges/htx"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/exchanges/hyperliquid"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/exchanges/kraken"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/exchanges/kucoin"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/exchanges/lighter"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/exchanges/mexc"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/exchanges/okx"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/exchanges/paradex"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/exchanges/whitebit"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/arb"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding"
	faster "github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding/aster"
	fbinance "github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding/binance"
	fbingx "github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding/bingx"
	fbitget "github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding/bitget"
	fbybit "github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding/bybit"
	fgate "github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding/gate"
	fhtx "github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding/htx"
	fhyperliquid "github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding/hyperliquid"
	fkucoin "github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding/kucoin"
	fmexc "github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding/mexc"
	fokx "github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding/okx"
	fwhitebit "github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding/whitebit"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/redisbus"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/symbols"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
	// Trade-adapter blank imports — each package self-registers in
	// init() so we never have to mention them outside of import.
	_ "github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade/binance"
	_ "github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade/bybit"
	_ "github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade/okx"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/wsbroadcast"
	"net/http"
)

func main() {
	cfg := config.Load()
	log.Init(cfg.LogLevel)
	l := log.L()

	l.Info().
		Str("cache_dir", cfg.CacheDir).
		Bool("redis_enabled", cfg.RedisURL != "").
		Int("prewarm_top_n", cfg.PrewarmTopN).
		Strs("worker_exchanges", cfg.WorkerExchanges).
		Msg("avalant-fetcher starting")

	store := cache.New()
	dumper := cache.NewDumper(store, cfg.CacheDir, cfg.FileDumpInterval)

	fundingStore := funding.NewStore()
	fundingDumper := funding.NewDumper(fundingStore, cfg.CacheDir, 500*time.Millisecond)
	// HTX-class venues that report rate-only via REST inherit mark from
	// the orderbook midprice — Phase 4 cross-pollination.
	fundingDumper.SetOrderbookSource(func(ex, sym string) (float64, float64, bool) {
		e, ok := store.Get(ex, sym)
		if !ok || len(e.Bids) == 0 || len(e.Asks) == 0 {
			return 0, 0, false
		}
		return e.Bids[0][0], e.Asks[0][0], true
	})

	mgr := symbols.New()

	// Redis pub/sub bridge — book:subscribe / book:unsubscribe events
	// from Python web roles route into the SymbolManager so /arb pair
	// pages get on-demand subscribe behaviour.
	subscriber, err := redisbus.NewSubscriber(cfg.RedisURL, mgr)
	if err != nil {
		l.Warn().Err(err).Msg("redis subscriber disabled — falling back to prewarm-only")
	}
	defer func() {
		if subscriber != nil {
			_ = subscriber.Close()
		}
	}()

	// Redis writer — mirror every orderbook update into ob:<ex>:<sym>
	// keys (TTL 10s) matching Python's orderbook_redis.py write shape.
	// This is the cutover-critical path: Python web's /orderbook reads
	// Redis first, file second. Mirroring makes the cutover instant —
	// once Go is writing to Redis, stopping Python fetcher is safe.
	writer, werr := redisbus.NewWriter(cfg.RedisURL, cfg.RedisWriteThrottle)
	if werr != nil {
		l.Warn().Err(werr).Msg("redis writer disabled")
	}
	defer func() {
		if writer != nil {
			_ = writer.Close()
		}
	}()
	if writer != nil {
		store.SetOnUpdate(func(ex, sym string, bids, asks []ws.Level) {
			writer.WriteBook(ex, sym, bids, asks)
		})
	}

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	g, gctx := errgroup.WithContext(ctx)

	// Orderbook file dumper.
	g.Go(func() error {
		err := dumper.Run(gctx)
		if err != nil && err != context.Canceled {
			return err
		}
		return nil
	})

	// Funding file dumper (writes funding.<ex>.json + funding.json).
	g.Go(func() error {
		err := fundingDumper.Run(gctx)
		if err != nil && err != context.Canceled {
			return err
		}
		return nil
	})

	// Futures arb compute — port of Python's arbitrage_service.py. Reads
	// the funding store, builds cross-venue opportunities, writes
	// arbitrage.json every 700ms (matches AVALANT_ARB_CACHE_TTL on prod).
	arbCompute := arb.NewCompute(fundingStore, store, cfg.CacheDir, 500*time.Millisecond)
	g.Go(func() error {
		return arbCompute.Run(gctx)
	})

	// Spot arb compute — Python's spot_arbitrage_service. REST tickers
	// from 9 spot venues + funding store join → spot_arbitrage.json
	// every 2s.
	spotCompute := arb.NewSpotCompute(fundingStore, store, cfg.CacheDir, 1*time.Second)
	g.Go(func() error {
		return spotCompute.Run(gctx)
	})

	// DEX arb compute — Python's dex_arbitrage_service port. CoinGecko
	// symbol→contract cache (1h TTL) + DexScreener pool fetches with
	// cross-pool consensus check. Writes dex_arbitrage.json every 30s.
	dexCompute := arb.NewDEXCompute(fundingStore, store, cfg.CacheDir, 30*time.Second)
	g.Go(func() error {
		return dexCompute.Run(gctx)
	})

	// WS broadcaster for /api/screener/ws/* — drains arbitrage.json,
	// computes diff vs last broadcast, fans out to connected clients.
	// nginx routes the /api/screener/ws/* path family at this port;
	// everything else stays on the Python app. Disabled when
	// AVALANT_WS_BROADCAST_PORT is empty / unset.
	if cfg.WSBroadcastPort != "" {
		secret := os.Getenv("SECRET_KEY")
		if secret == "" {
			l.Warn().Msg("SECRET_KEY unset — WS broadcaster will reject every authed connection")
		}
		// Redis reader for /ws/book — same connection family as the
		// writer, but a separate client so the read MGET path doesn't
		// share a connection slot with the chatty per-update writes.
		bookReader, brErr := redisbus.NewReader(cfg.RedisURL)
		if brErr != nil {
			l.Warn().Err(brErr).Msg("redis reader init failed — /ws/book will fall back to in-process cache")
		}
		defer func() {
			if bookReader != nil {
				_ = bookReader.Close()
			}
		}()
		wsSvc := wsbroadcast.NewService(
			wsbroadcast.NewJWTValidator(secret),
			wsbroadcast.NewLongShort(cfg.CacheDir),
			wsbroadcast.NewFunding(cfg.CacheDir),
			wsbroadcast.NewBook(bookReader, store, mgr),
		)
		mux := http.NewServeMux()
		wsSvc.Routes(mux)
		// Trade-engine internal HTTP routes mounted on the same listener.
		// Reachable only from the Python web role over the docker-compose
		// network — nginx never proxies /internal/*. Auth-gated by the
		// AVALANT_INTERNAL_SECRET shared header.
		trade.Routes(mux)
		srv := &http.Server{Addr: ":" + cfg.WSBroadcastPort, Handler: mux}
		g.Go(func() error {
			wsSvc.Run(gctx)
			return nil
		})
		g.Go(func() error {
			l.Info().Str("addr", srv.Addr).Msg("ws-broadcaster listening")
			errCh := make(chan error, 1)
			go func() { errCh <- srv.ListenAndServe() }()
			select {
			case <-gctx.Done():
				shCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
				defer cancel()
				_ = srv.Shutdown(shCtx)
				return nil
			case err := <-errCh:
				if err != nil && err != http.ErrServerClosed {
					l.Error().Err(err).Msg("ws-broadcaster server exited")
					return err
				}
				return nil
			}
		})
	}

	// Symbol manager reconciliation loop.
	g.Go(func() error {
		mgr.Run(gctx)
		return nil
	})

	// Tracked-set auto-touch is intentionally disabled. The previous
	// version called Manager.TouchFromArbFiles every 30 s for the full
	// 1000-pair tracked set, which caused a flood of SetSymbols calls
	// across all 24 venue runners. Adapters with strict rate limits
	// (Binance public WS at 5 msg/s, Aster as a fork) ended up in a
	// 1008 policy-close loop because the cumulative SUBSCRIBE volume
	// blew past their thresholds even with chunking + 250 ms delay.
	//
	// User-touch from the web (POST /api/screener/in-out → file +
	// pub/sub bridge → mgr.Touch) covers the same ground at a
	// natural cadence: 256 keys per 3 s tick, scoped to whatever the
	// user is actively viewing. The manager's IdleWindow drops
	// stale touches; freshly-touched pairs subscribe within one
	// reconcile (5 s).
	//
	// `TouchFromArbFiles` stays as a function for future use (e.g.,
	// when adapters get per-venue split connections to absorb the
	// bigger churn).

	// Redis subscriber (skipped silently if REDIS_URL unset).
	if subscriber != nil {
		g.Go(func() error {
			subscriber.Run(gctx)
			return nil
		})
	}

	// Funding adapters — all 12 venues. Each registered with the
	// SymbolManager so prewarm + user-touch flow applies uniformly.
	for _, fa := range []funding.Adapter{
		fbinance.New(),
		fbybit.New(),
		fokx.New(),
		fbitget.New(),
		faster.New(),
		fgate.New(),
		fkucoin.New(),
		fmexc.New(),
		fbingx.New(),
		fhtx.New(),
		fhyperliquid.New(),
		fwhitebit.New(),
	} {
		runner := funding.NewRunner(fa, fundingStore)
		mgr.RegisterFunding(fa.Name(), runner)
		g.Go(func() error {
			runner.Run(gctx)
			return nil
		})
	}

	// Cache pruner — every 60s, drops symbols not requested in
	// IdleTimeout. Mirrors Python's "orderbook poller idle, stopping" log.
	g.Go(func() error {
		t := time.NewTicker(60 * time.Second)
		defer t.Stop()
		for {
			select {
			case <-gctx.Done():
				return nil
			case <-t.C:
				if n := store.Prune(cfg.IdleTimeout); n > 0 {
					l.Info().Int("removed", n).Msg("cache prune")
				}
			}
		}
	})

	// Orderbook adapters. SymbolManager.RegisterOrderbook + the initial
	// prewarm are the two inputs that decide what each runner subscribes
	// to; from there reconcile() drives all updates (every 5s).
	for _, e := range orderbookRegistry(cfg, store) {
		mgr.RegisterOrderbook(e.name, e.runner)
		runner := e.runner
		g.Go(func() error {
			runner.Run(gctx)
			return nil
		})
	}

	// Initial prewarm. During shadow-mode rollout we want Go's hot-list
	// to match Python's so the diff comparison is apples-to-apples; that
	// means reading from Python's cache dir (env BOOTSTRAP_FROM_DIR,
	// default /tmp/avalant_cache) rather than our own write dir. Falls
	// back to cfg.CacheDir, then Default20.
	bootstrapDir := os.Getenv("AVALANT_BOOTSTRAP_FROM_DIR")
	if bootstrapDir == "" {
		bootstrapDir = "/tmp/avalant_cache"
	}
	// Initial prewarm: cross-venue volume-rank top-N. Used until the
	// arb compute writes its first arbitrage.json (a few hundred ms
	// after start) — after that the per-venue arb-derived prewarm
	// takes over.
	startSymbols := bootstrap.TopSymbols(bootstrapDir, cfg.PrewarmTopN)
	if len(startSymbols) < 5 {
		startSymbols = bootstrap.TopSymbols(cfg.CacheDir, cfg.PrewarmTopN)
	}
	{
		head := startSymbols
		if len(head) > 20 {
			head = head[:20]
		}
		l.Info().Strs("bootstrap_symbols_first20", head).Int("total", len(startSymbols)).Str("from_dir", bootstrapDir).Msg("symbol bootstrap")
	}
	mgr.PrewarmAll(startSymbols)

	// Periodic prewarm refresh — every 60 s, sourced from the arb
	// output files. Per-venue prewarm = exactly the symbols that
	// appear as one of that venue's legs in the top-1000 arb opps
	// (futures + spot + dex), unioned with Default20 majors as a
	// floor so common pairs always stay subscribed.
	//
	// Why this works without the Phase B v1 churn: the prewarm set
	// changes by ~5-20 symbols per minute (only when the arb top
	// shuffles in/out a token), not 1000 in one go. SetSymbols sees
	// a small delta and the runners can keep up.
	g.Go(func() error {
		// Initial fire after 5s — gives arb compute time to produce
		// its first arbitrage.json so we don't blank-prewarm.
		t := time.NewTicker(60 * time.Second)
		defer t.Stop()
		select {
		case <-gctx.Done():
			return nil
		case <-time.After(5 * time.Second):
		}
		mgr.PrewarmFromArbFiles(cfg.CacheDir, bootstrap.Default20)
		for {
			select {
			case <-gctx.Done():
				return nil
			case <-t.C:
				mgr.PrewarmFromArbFiles(cfg.CacheDir, bootstrap.Default20)
			}
		}
	})

	if err := g.Wait(); err != nil {
		l.Error().Err(err).Msg("fetcher exited with error")
		os.Exit(1)
	}
	l.Info().Msg("avalant-fetcher stopped cleanly")
}

// orderbookEntry pairs a venue name with its instantiated Runner so the
// Symbol manager can route subscribe events by name.
type orderbookEntry struct {
	name   string
	runner *ws.Runner
}

// orderbookRegistry instantiates every registered venue's WS adapter and
// returns them paired with the venue name. Filtered by cfg.WorkerExchanges
// when set (allows per-replica sharding).
func orderbookRegistry(cfg config.Config, store *cache.Store) []orderbookEntry {
	type spec struct {
		name    string
		factory func() *ws.Runner
	}

	all := []spec{
		// Phase 1 + 2 — all 16 orderbook WS adapters. Each implements
		// ws.Adapter and addresses every applicable bug from PLAN.md by
		// design. Order matches PLAN.md sequencing (simple → complex).
		{name: "binance", factory: func() *ws.Runner { return binance.NewFutures(store) }},
		{name: "bybit", factory: func() *ws.Runner { return bybit.NewFutures(store) }},
		{name: "okx", factory: func() *ws.Runner { return okx.NewFutures(store) }},
		{name: "aster", factory: func() *ws.Runner { return aster.NewFutures(store) }},
		{name: "gate", factory: func() *ws.Runner { return gate.NewFutures(store) }},
		{name: "mexc", factory: func() *ws.Runner { return mexc.NewFutures(store) }},
		{name: "whitebit", factory: func() *ws.Runner { return whitebit.NewFutures(store) }},
		{name: "bingx", factory: func() *ws.Runner { return bingx.NewFutures(store) }},
		{name: "htx", factory: func() *ws.Runner { return htx.NewFutures(store) }},
		{name: "kraken", factory: func() *ws.Runner { return kraken.NewFutures(store) }},
		{name: "kucoin", factory: func() *ws.Runner { return kucoin.NewFutures(store) }},
		{name: "bitget", factory: func() *ws.Runner { return bitget.NewFutures(store) }},
		{name: "bitget_spot", factory: func() *ws.Runner { return bitget.NewSpot(store) }},
		// Phase 8 — spot orderbook coverage for the rest of the 8-venue
		// spot-arb set. Same WS hosts/families as the futures adapters
		// where possible; KuCoin and BingX needed separate spot.go files
		// because the WS host differs from futures.
		{name: "binance_spot", factory: func() *ws.Runner { return binance.NewSpot(store) }},
		{name: "bybit_spot", factory: func() *ws.Runner { return bybit.NewSpot(store) }},
		{name: "okx_spot", factory: func() *ws.Runner { return okx.NewSpot(store) }},
		{name: "gate_spot", factory: func() *ws.Runner { return gate.NewSpot(store) }},
		{name: "kucoin_spot", factory: func() *ws.Runner { return kucoin.NewSpot(store) }},
		{name: "bingx_spot", factory: func() *ws.Runner { return bingx.NewSpot(store) }},
		// Phase 8b — spot for the rest of the CEXes that have a spot
		// product. MEXC spot is on the wbs-api.mexc.com Protobuf endpoint
		// and Hyperliquid spot uses @<index> pair IDs that need a
		// spotMeta REST seed; both deferred to a follow-up.
		{name: "htx_spot", factory: func() *ws.Runner { return htx.NewSpot(store) }},
		{name: "whitebit_spot", factory: func() *ws.Runner { return whitebit.NewSpot(store) }},
		{name: "kraken_spot", factory: func() *ws.Runner { return kraken.NewSpot(store) }},
		{name: "backpack_spot", factory: func() *ws.Runner { return backpack.NewSpot(store) }},
		// Perp DEX with a spot product. Hyperliquid is the only one in
		// the current set; the rest (paradex, lighter, etc.) are
		// derivatives-only.
		{name: "hyperliquid_spot", factory: func() *ws.Runner { return hyperliquid.NewSpot(store) }},
		{name: "hyperliquid", factory: func() *ws.Runner { return hyperliquid.NewFutures(store) }},
		{name: "paradex", factory: func() *ws.Runner { return paradex.NewFutures(store) }},
		{name: "lighter", factory: func() *ws.Runner { return lighter.NewFutures(store) }},
		{name: "backpack", factory: func() *ws.Runner { return backpack.NewFutures(store) }},
	}

	want := func(name string) bool {
		if len(cfg.WorkerExchanges) == 0 {
			return true
		}
		for _, w := range cfg.WorkerExchanges {
			if w == name {
				return true
			}
		}
		return false
	}

	out := make([]orderbookEntry, 0, len(all))
	for _, s := range all {
		if want(s.name) {
			out = append(out, orderbookEntry{name: s.name, runner: s.factory()})
		}
	}
	return out
}
