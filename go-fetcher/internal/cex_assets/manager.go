package cex_assets

import (
	"context"
	"net/http"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
)

// refreshInterval — how often the manager re-fetches every venue's
// asset list. 24h matches the user's policy: addresses change roughly
// once per year per project, so daily polling is well-overkill for
// freshness but tight enough to catch new listings within a day.
const refreshInterval = 24 * time.Hour

// initialDelay — wait briefly on start so the rest of the fetcher boot
// finishes first (orderbook WS, funding feed, arb compute) before the
// (relatively heavy) 3-venue asset sweep runs.
const initialDelay = 30 * time.Second

// fetcher is one venue's adapter — same shape for all three public
// adapters so the manager can iterate them generically.
type fetcher struct {
	venue string
	fn    func(context.Context, *http.Client) (VenueAssets, error)
}

// Manager schedules periodic refreshes for every public adapter and
// writes the merged snapshot to disk. Run as a goroutine via main.go.
type Manager struct {
	reg      *Registry
	client   *http.Client
	cacheDir string

	mu        sync.Mutex
	fetchers  []fetcher
}

// NewManager constructs a manager with the three public adapters wired
// (gate, kucoin, bitget). Signed adapters can be added later via
// AddFetcher when their env keys are present.
func NewManager(reg *Registry, cacheDir string) *Manager {
	m := &Manager{
		reg:      reg,
		cacheDir: cacheDir,
		client: &http.Client{
			Timeout: 20 * time.Second,
		},
	}
	m.fetchers = []fetcher{
		{venue: "gate", fn: FetchGate},
		{venue: "kucoin", fn: FetchKuCoin},
		{venue: "bitget", fn: FetchBitget},
	}
	return m
}

// Run blocks until ctx is cancelled. Performs one initial refresh after
// initialDelay, then refreshes on refreshInterval. Individual venue
// failures are logged and don't block other venues — the registry
// keeps whatever it had for the failed venue.
func (m *Manager) Run(ctx context.Context) {
	// Defer the first sweep so boot stays snappy.
	select {
	case <-ctx.Done():
		return
	case <-time.After(initialDelay):
	}
	m.refreshAll(ctx)
	t := time.NewTicker(refreshInterval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			m.refreshAll(ctx)
		}
	}
}

// refreshAll fetches every adapter sequentially (cheap; 3 calls), then
// persists one merged snapshot.
func (m *Manager) refreshAll(ctx context.Context) {
	m.mu.Lock()
	fetchers := append([]fetcher(nil), m.fetchers...)
	m.mu.Unlock()

	for _, f := range fetchers {
		fctx, cancel := context.WithTimeout(ctx, 25*time.Second)
		assets, err := f.fn(fctx, m.client)
		cancel()
		if err != nil {
			log.L().Warn().Str("venue", f.venue).Err(err).Msg("cex_assets refresh failed")
			continue
		}
		m.reg.SetVenue(f.venue, assets)
		log.L().Info().Str("venue", f.venue).Int("tickers", len(assets)).Msg("cex_assets refreshed")
	}
	if err := m.reg.PersistToDisk(); err != nil {
		log.L().Warn().Err(err).Msg("cex_assets persist failed")
	}
}
