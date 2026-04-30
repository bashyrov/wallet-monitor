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

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/config"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
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

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	g, gctx := errgroup.WithContext(ctx)

	// File dumper.
	g.Go(func() error {
		err := dumper.Run(gctx)
		if err != nil && err != context.Canceled {
			return err
		}
		return nil
	})

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

	// WS adapters — populated in Phase 1+.
	for _, runner := range buildRunners(cfg, store) {
		runner := runner
		g.Go(func() error {
			runner.Run(gctx)
			return nil
		})
	}

	if err := g.Wait(); err != nil {
		l.Error().Err(err).Msg("fetcher exited with error")
		os.Exit(1)
	}
	l.Info().Msg("avalant-fetcher stopped cleanly")
}

// buildRunners — registry of all adapters. Empty in Phase 0; Phase 1 adds
// Binance/Bybit/OKX, Phase 2 the rest. Filtered by cfg.WorkerExchanges
// when set (allows per-replica sharding).
func buildRunners(cfg config.Config, store *cache.Store) []*ws.Runner {
	type entry struct {
		name    string
		factory func() *ws.Runner
	}

	registry := []entry{
		// Phase 1 — added in next commit.
		// {name: "binance", factory: func() *ws.Runner { return binance.NewFutures(store) }},
		// {name: "bybit",   factory: func() *ws.Runner { return bybit.NewFutures(store) }},
		// {name: "okx",     factory: func() *ws.Runner { return okx.NewFutures(store) }},
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

	out := make([]*ws.Runner, 0, len(registry))
	for _, e := range registry {
		if want(e.name) {
			out = append(out, e.factory())
		}
	}
	return out
}
