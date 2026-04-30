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
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
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

	// Symbol manager reconciliation loop.
	g.Go(func() error {
		mgr.Run(gctx)
		return nil
	})

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
	startSymbols := bootstrap.TopSymbols(bootstrapDir, cfg.PrewarmTopN)
	if len(startSymbols) < 5 {
		startSymbols = bootstrap.TopSymbols(cfg.CacheDir, cfg.PrewarmTopN)
	}
	l.Info().Strs("bootstrap_symbols", startSymbols).Str("from_dir", bootstrapDir).Msg("symbol bootstrap")
	mgr.PrewarmAll(startSymbols)

	// Periodic prewarm refresh — pick up new top-N from Python's
	// funding.json every 30s. Once Go owns the funding compute (already
	// does — it writes funding.json itself) and Python is decommissioned,
	// this becomes a no-op. For now, the shadow mode benefits from
	// staying in sync with Python's hot list.
	g.Go(func() error {
		t := time.NewTicker(30 * time.Second)
		defer t.Stop()
		for {
			select {
			case <-gctx.Done():
				return nil
			case <-t.C:
				syms := bootstrap.TopSymbols(bootstrapDir, cfg.PrewarmTopN)
				if len(syms) >= 5 {
					mgr.PrewarmAll(syms)
				}
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
