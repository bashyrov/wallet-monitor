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

// fetcher is one venue's adapter — same shape for the public (creds-less)
// and signed (env-credential) variants so the manager can iterate them
// generically. signedFn wraps the per-venue signature flow; for public
// adapters, signedFn is nil and fn is used directly.
type fetcher struct {
	venue string
	// fn used for public adapters (gate/kucoin/bitget); nil when signed.
	fn func(context.Context, *http.Client) (VenueAssets, error)
	// signedFn used for signed adapters (binance/bybit/okx/mexc/bingx);
	// nil when public. SignedCreds is loaded by the manager from the env
	// at the START of each refresh — picking up newly-added .env keys
	// without a process restart (24h interval makes this nearly free).
	signedFn func(context.Context, *http.Client, SignedCreds) (VenueAssets, error)
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
		// Public, always run — coverage tier 1.
		{venue: "gate", fn: FetchGate},
		{venue: "kucoin", fn: FetchKuCoin},
		{venue: "bitget", fn: FetchBitget},
		{venue: "whitebit", fn: FetchWhiteBIT}, // chain names + deposit/withdraw flags (no addr)
		{venue: "backpack", fn: FetchBackpack}, // full addr + per-network flags
		// Signed, hybrid — coverage tier 2. signedFn is called only when
		// LoadSignedCreds returns HasKey()==true for this venue. No key
		// → manager logs INFO("skipped, no creds") and moves on.
		{venue: "binance", signedFn: FetchBinance},
		{venue: "bybit", signedFn: FetchBybit},
		{venue: "okx", signedFn: FetchOKX},
		{venue: "mexc", signedFn: FetchMEXC},
		{venue: "bingx", signedFn: FetchBingX},
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

// refreshAll fetches every adapter sequentially (cheap; up to 8 calls),
// then persists one merged snapshot. Hybrid policy:
//   public adapter (fn != nil)        → always run
//   signed adapter (signedFn != nil)  → run iff env keys are present
//                                       (LoadSignedCreds.HasKey()).
// Skipped venues stay in registry with whatever data they had — so a
// newly-removed key doesn't wipe stale-but-useful data, and a brand-new
// venue stays empty until the first successful fetch.
func (m *Manager) refreshAll(ctx context.Context) {
	m.mu.Lock()
	fetchers := append([]fetcher(nil), m.fetchers...)
	m.mu.Unlock()

	for _, f := range fetchers {
		var (
			assets VenueAssets
			err    error
		)
		fctx, cancel := context.WithTimeout(ctx, 25*time.Second)
		switch {
		case f.fn != nil:
			assets, err = f.fn(fctx, m.client)
		case f.signedFn != nil:
			creds := LoadSignedCreds(f.venue)
			// OKX additionally requires passphrase — its adapter
			// checks creds.Passphrase itself and returns a clear
			// error. For all others HasKey() is sufficient.
			if !creds.HasKey() {
				log.L().Info().Str("venue", f.venue).Msg("cex_assets signed adapter SKIPPED (no env keys — set CEX_<VENUE>_READ_KEY / _SECRET to enable)")
				cancel()
				continue
			}
			assets, err = f.signedFn(fctx, m.client, creds)
		}
		cancel()
		if err != nil {
			// Error messages are venue + status only — adapters scrub
			// keys before returning. Don't add fields that could echo
			// the secret accidentally.
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
