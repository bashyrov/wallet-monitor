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

// TopSymbols returns up to `n` symbols. We used to try to read top-N
// from Python's funding.json but the schema there is per-(symbol,
// exchange) row with no per-symbol aggregate spread — so picking
// top-N from there gave arbitrary symbols and hit "subscribe to a
// coin that doesn't exist on Hyperliquid/Bybit" → broken pipes.
//
// Default20 is hand-picked majors that exist on all 16 venues. Once
// arb compute lands in the Go fetcher itself, the prewarm becomes
// data-driven.
func TopSymbols(cacheDir string, n int) []string {
	if n >= len(Default20) {
		return Default20
	}
	return Default20[:n]
}

func readFromFunding(cacheDir string, n int) []string {
	path := filepath.Join(cacheDir, "funding.json")
	data, err := os.ReadFile(path)
	if err != nil {
		return nil
	}

	// Python writes funding.json as:
	//   { "rows": [{ "symbol": "...", "rates": {...}, "spread_pct": ... }, ...] }
	// We use spread_pct (abs) as the sort key — matches the prewarm
	// ranking in arbitrage_service.
	var doc struct {
		Rows []struct {
			Symbol     string  `json:"symbol"`
			SpreadPct  float64 `json:"spread_pct"`
		} `json:"rows"`
	}
	if err := sonic.Unmarshal(data, &doc); err != nil {
		return nil
	}
	if len(doc.Rows) == 0 {
		return nil
	}

	// Sort by abs(spread_pct) desc.
	sort.Slice(doc.Rows, func(i, j int) bool {
		return abs(doc.Rows[i].SpreadPct) > abs(doc.Rows[j].SpreadPct)
	})

	out := make([]string, 0, n)
	seen := make(map[string]struct{}, n)
	for _, r := range doc.Rows {
		if r.Symbol == "" {
			continue
		}
		if _, dup := seen[r.Symbol]; dup {
			continue
		}
		seen[r.Symbol] = struct{}{}
		out = append(out, r.Symbol)
		if len(out) >= n {
			break
		}
	}
	return out
}

func abs(x float64) float64 {
	if x < 0 {
		return -x
	}
	return x
}
