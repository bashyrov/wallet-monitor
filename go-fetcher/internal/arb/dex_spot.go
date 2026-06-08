// dex_spot.go — DEX↔CEX spot-only arbitrage compute.
//
// Both legs are SPOT: DexScreener pool price on one side, CEX spot-
// ticker price on the other. Arbitrages the price gap of the same
// token between the two venues. Direction is bidirectional:
//
//   dex_to_cex: DEX cheaper (dex_price < cex_spot) → buy DEX, sell CEX
//   cex_to_dex: CEX cheaper (cex_spot < dex_price) → buy CEX, sell DEX
//
// Spread metric (symmetric, mid-anchored):
//
//   mid       = (cex_spot + dex_price) / 2
//   spread%   = (cex_spot - dex_price) / mid * 100      (signed)
//   |spread%| is the absolute basis used for ranking.
//
// No funding, no perp. This is a moment-in-time price arbitrage, NOT a
// hold-position basis trade — don't carry funding/UPNL fields.
//
// Data sources are SHARED with the existing engines to avoid double-
// loading rate-limited backends:
//
//   - DexScreener prices: snapshot from DEXCompute (refreshed every 30s,
//     same DexScreener fetches dex_short uses)
//   - CEX spot tickers: snapshot from SpotCompute (refreshed every 500ms,
//     same 9-venue REST it polls for spot_short)
//
// Behind AVALANT_DEX_SPOT=1. Off = goroutine never spawned, no output
// file written, no extra load on the data sources beyond what already
// runs for dex_short + spot_short.
package arb

import (
	"context"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
)

// DexSpotCompute polls the shared DEX + spot snapshots and emits
// dex_spot_arbitrage.json on each tick.
type DexSpotCompute struct {
	dex      *DEXCompute   // SnapshotDexBySym() — DEX prices per symbol
	spot     *SpotCompute  // SnapshotSpotMap() — CEX spot tickers per (sym, venue)
	cacheDir string
	interval time.Duration

	mu        sync.Mutex
	firstSeen map[dexSpotKey]time.Time
	lastSeen  map[dexSpotKey]time.Time

	// cexMatcher is set when AVALANT_CEX_ASSETS=1 (same registry that
	// dex_short uses). nil → every row emits address_verified=false.
	cexMatcher CexAddressMatcher
}

// SetCexRegistry wires the address-match closure.
func (c *DexSpotCompute) SetCexRegistry(m CexAddressMatcher) {
	c.mu.Lock()
	c.cexMatcher = m
	c.mu.Unlock()
}

type dexSpotKey struct {
	symbol string
	cexEx  string
}

// Tuning constants — borrowed from dex.go where the semantics are the
// same, and from spot.go for the spot-fee table.
const (
	dexSpotOppMinLifetime = 25 * time.Second
	dexSpotOppPurgeAfter  = 5 * time.Minute
	dexSpotMaxBasisPct    = 100.0 // collision guard — same threshold as dex_short
	dexSpotFeeRoundtrip   = dexFeeRoundtripPct
	dexSpotMinDEXLiqUSD   = minDEXLiqUSD
)

// NewDexSpotCompute wires the new compute against the existing DEX +
// spot engines. `dex` and `spot` MUST be non-nil — they're the data
// sources. interval should match dex's interval (30s) since DexScreener
// is the slow leg.
func NewDexSpotCompute(dex *DEXCompute, spot *SpotCompute, cacheDir string, interval time.Duration) *DexSpotCompute {
	return &DexSpotCompute{
		dex:       dex,
		spot:      spot,
		cacheDir:  cacheDir,
		interval:  interval,
		firstSeen: make(map[dexSpotKey]time.Time, 1024),
		lastSeen:  make(map[dexSpotKey]time.Time, 1024),
	}
}

// Run loops on c.interval until ctx is cancelled. The first tick waits
// 12s — DexScreener has its own warm-up plus we want SpotCompute to have
// produced at least one snapshot.
func (c *DexSpotCompute) Run(ctx context.Context) error {
	t := time.NewTicker(c.interval)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-time.After(12 * time.Second):
	}
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

func (c *DexSpotCompute) tick() {
	dexBySym := c.dex.SnapshotDexBySym()
	if len(dexBySym) == 0 {
		// Empty file so downstream readers (REST, WS broadcaster) see
		// structure even before the first DEX cycle completes.
		c.writeEmpty()
		return
	}
	spotMap := c.spot.SnapshotSpotMap()
	if len(spotMap) == 0 {
		c.writeEmpty()
		return
	}

	now := time.Now()
	c.mu.Lock()
	cutoff := now.Add(-dexSpotOppPurgeAfter)
	for k, ts := range c.lastSeen {
		if ts.Before(cutoff) {
			delete(c.firstSeen, k)
			delete(c.lastSeen, k)
		}
	}
	c.mu.Unlock()

	opps := make([]map[string]any, 0, 512)
	cexHits := 0
	for sym, dex := range dexBySym {
		if dex.Price <= 0 || dex.LiquidityUSD < dexSpotMinDEXLiqUSD {
			continue
		}
		spotByEx, ok := spotMap[sym]
		if !ok {
			continue
		}
		for cexEx, st := range spotByEx {
			if st.Price <= 0 {
				continue
			}
			mid := (st.Price + dex.Price) / 2.0
			if mid <= 0 {
				continue
			}
			spreadPct := (st.Price - dex.Price) / mid * 100.0
			// Collision filter — same |basis| guard dex_short uses.
			if spreadPct > dexSpotMaxBasisPct || spreadPct < -dexSpotMaxBasisPct {
				continue
			}
			// Hysteresis: same pattern as dex_short. Avoids a hit on first
			// observation from leaking into a published opp before the
			// pair has demonstrated stable existence.
			key := dexSpotKey{symbol: sym, cexEx: cexEx}
			c.mu.Lock()
			first, seen := c.firstSeen[key]
			if !seen {
				c.firstSeen[key] = now
				c.lastSeen[key] = now
				c.mu.Unlock()
				continue
			}
			c.lastSeen[key] = now
			c.mu.Unlock()
			if now.Sub(first) < dexSpotOppMinLifetime {
				continue
			}
			direction := "dex_to_cex" // DEX cheaper → buy DEX, sell CEX
			if spreadPct < 0 {
				direction = "cex_to_dex"
			}
			absSpread := spreadPct
			if absSpread < 0 {
				absSpread = -absSpread
			}
			feeDexRT := dexSpotFeeRoundtrip
			feeCexRT := spotFeeOf(cexEx) * 100.0 * 2.0
			totalFees := feeDexRT + feeCexRT
			netPct := absSpread - totalFees
			// Address verification — same policy as dex_short. With
			// AVALANT_CEX_ASSETS=1, gate/kucoin/bitget rows are verified
			// when (chain, address) match; every other venue (incl.
			// htx and the 5 signed venues without keys) returns
			// AddressKnown=false → verified=false → UI ⚠ unverified.
			verified, matchChain, addrKnown := false, "", false
			if c.cexMatcher != nil {
				verified, matchChain, addrKnown = c.cexMatcher(cexEx, sym, dex.Chain, dex.BaseAddress)
			}
			cexHits++
			opps = append(opps, map[string]any{
				"type":                "dex_spot",
				"symbol":              sym,
				"direction":           direction,
				"dex_chain":           dex.Chain,
				"dex_name":            dex.Dex,
				"dex_pair_url":        dex.PairURL,
				"dex_base_address":    dex.BaseAddress,
				"cex_exchange":        cexEx,
				"dex_price":           dex.Price,
				"cex_spot_price":      st.Price,
				"dex_liquidity_usd":   dex.LiquidityUSD,
				"dex_volume_usd":      dex.VolumeUSD,
				"cex_volume_usd":      st.VolumeUSD,
				"spread_pct":          spreadPct, // signed
				"abs_spread_pct":      absSpread, // ranking key
				"fee_dex":             feeDexRT,
				"fee_cex":             feeCexRT,
				"total_fees":          totalFees,
				"net_pct":             netPct,
				"address_verified":    verified,
				"address_match_chain": matchChain,
				"address_known":       addrKnown,
			})
		}
	}

	// Sort by |spread| descending — the cleanest entry is whichever pair
	// has the widest gap right now.
	sort.Slice(opps, func(i, j int) bool {
		ai, _ := opps[i]["abs_spread_pct"].(float64)
		aj, _ := opps[j]["abs_spread_pct"].(float64)
		return ai > aj
	})
	if len(opps) > 200 {
		opps = opps[:200]
	}

	cexUniverse := make(map[string]struct{}, 16)
	for _, byEx := range spotMap {
		for ex := range byEx {
			cexUniverse[ex] = struct{}{}
		}
	}
	cexList := make([]string, 0, len(cexUniverse))
	for ex := range cexUniverse {
		cexList = append(cexList, ex)
	}
	sort.Strings(cexList)

	out := map[string]any{
		"opportunities":   opps,
		"generated_at":    now.Unix(),
		"symbols_scanned": len(dexBySym),
		"cex_hits":        cexHits,
		"cex_exchanges":   cexList,
	}
	log.L().Info().
		Int("scanned", len(dexBySym)).
		Int("cex_hits", cexHits).
		Int("opps", len(opps)).
		Msg("dex_spot cycle complete")
	if err := writeAtomic(filepath.Join(c.cacheDir, "dex_spot_arbitrage.json"), out); err != nil {
		log.L().Warn().Err(err).Msg("dex_spot_arbitrage write failed")
	}
}

func (c *DexSpotCompute) writeEmpty() {
	out := map[string]any{
		"opportunities":   []any{},
		"generated_at":    time.Now().Unix(),
		"symbols_scanned": 0,
		"cex_hits":        0,
		"cex_exchanges":   []string{},
	}
	_ = writeAtomic(filepath.Join(c.cacheDir, "dex_spot_arbitrage.json"), out)
}

// silence the unused-import linter if strings is not referenced
// elsewhere in this file (compile-time sanity for refactors).
var _ = strings.ToLower
