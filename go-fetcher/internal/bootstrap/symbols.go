// Package bootstrap supplies a starting symbol set for each WS runner
// before the prewarm/redis-subscribe wiring lands (Phase 4).
//
// Strategy:
//
//  1. If the cache dir contains Python's funding.json, read the top-N
//     symbols ordered by abs(funding rate) — same hot list Python uses.
//     This makes the diff comparison apples-to-apples during parallel run.
//  2. Otherwise fall back to a hardcoded top-20 (always available so a
//     fresh dev box can run the binary without any Python infra).
package bootstrap

import (
	"os"
	"path/filepath"
	"sort"

	"github.com/bytedance/sonic"
)

// Default20 — well-liquid major+majors. Used when funding.json is absent
// or unparseable.
var Default20 = []string{
	"BTC", "ETH", "SOL", "BNB", "XRP",
	"ADA", "DOGE", "AVAX", "LINK", "DOT",
	"MATIC", "UNI", "LTC", "BCH", "ATOM",
	"FIL", "ETC", "NEAR", "ICP", "APT",
}

// TopSymbols returns up to `n` symbols ranked by max 24h volume across
// all exchanges. Reads funding.json (written by funding/files.go) which
// contains a row per (symbol, exchange). Falls back to Default20 when
// the file is absent (cold start) or unparseable.
//
// Why this exists:
//   - At small n (≤20) Default20 is fine — those are the universal
//     majors that exist on every venue.
//   - At larger n (e.g. 1000) we need actual top-N by liquidity so
//     prewarm covers what users will see in the screener. Volume rank
//     is the right metric: a token with no volume isn't worth keeping
//     in the WS subscription set even if some venue lists it.
func TopSymbols(cacheDir string, n int) []string {
	if got := readFromFunding(cacheDir, n); len(got) >= 5 {
		return got
	}
	if n >= len(Default20) {
		return Default20
	}
	return Default20[:n]
}

// readFromFunding ranks symbols by max(volume_usd_24h) across all
// venues that quote them. Returns up to n unique symbols, descending.
func readFromFunding(cacheDir string, n int) []string {
	path := filepath.Join(cacheDir, "funding.json")
	data, err := os.ReadFile(path)
	if err != nil {
		return nil
	}

	// funding.json shape (per-(symbol, exchange) row):
	//   { "rows": [{"symbol":"BTC","exchange":"binance","volume_usd":...,
	//               "rate":..., "next_ts":..., "interval_h":...,
	//               "price":..., "apr":..., "cross_listed":...}, ...] }
	var doc struct {
		Rows []struct {
			Symbol    string  `json:"symbol"`
			VolumeUSD float64 `json:"volume_usd"`
		} `json:"rows"`
	}
	if err := sonic.Unmarshal(data, &doc); err != nil {
		return nil
	}
	if len(doc.Rows) == 0 {
		return nil
	}

	// Aggregate: for each symbol take the max volume across the venues
	// that list it. Median or sum could also work, but max best
	// represents "this symbol has somewhere to trade".
	maxVol := make(map[string]float64, len(doc.Rows))
	for _, r := range doc.Rows {
		if r.Symbol == "" {
			continue
		}
		if v := r.VolumeUSD; v > maxVol[r.Symbol] {
			maxVol[r.Symbol] = v
		}
	}
	type sv struct {
		sym string
		vol float64
	}
	ranked := make([]sv, 0, len(maxVol))
	for s, v := range maxVol {
		if v <= 0 {
			continue // no volume info — won't make a usable WS subscription
		}
		ranked = append(ranked, sv{sym: s, vol: v})
	}
	sort.Slice(ranked, func(i, j int) bool { return ranked[i].vol > ranked[j].vol })

	if n > len(ranked) {
		n = len(ranked)
	}
	out := make([]string, n)
	for i := 0; i < n; i++ {
		out[i] = ranked[i].sym
	}
	return out
}

func abs(x float64) float64 {
	if x < 0 {
		return -x
	}
	return x
}
