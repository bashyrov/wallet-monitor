package arb

import (
	"context"
	"path/filepath"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
)

// DEXCompute writes dex_arbitrage.json. For the initial Phase 7 cutover
// we ship a STUB that produces a valid-shaped empty file every 30s so
// Python web doesn't see "missing file" or stale Python content.
//
// Full port deferred — DEX needs CoinGecko mcap-rank cache + DexScreener
// per-contract fetcher (~600 LOC + a long-tail of contract-mapping
// quirks). For users this means /screener "DEX" tab shows empty until
// the next sprint, but the rest of the screener (long-short, spot-short)
// works on Go alone.
type DEXCompute struct {
	cacheDir string
	interval time.Duration
}

func NewDEXCompute(cacheDir string, interval time.Duration) *DEXCompute {
	return &DEXCompute{cacheDir: cacheDir, interval: interval}
}

func (c *DEXCompute) Run(ctx context.Context) error {
	t := time.NewTicker(c.interval)
	defer t.Stop()
	c.tick()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-t.C:
			c.tick()
		}
	}
}

func (c *DEXCompute) tick() {
	out := map[string]any{
		"opportunities":   []map[string]any{},
		"generated_at":    time.Now().Unix(),
		"symbols_scanned": 0,
		"dex_hits":        0,
	}
	if err := writeAtomic(filepath.Join(c.cacheDir, "dex_arbitrage.json"), out); err != nil {
		log.L().Warn().Err(err).Msg("dex_arbitrage write failed")
	}
}
