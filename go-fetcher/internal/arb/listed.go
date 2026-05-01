package arb

import (
	"context"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/bytedance/sonic"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
)

// listedCache holds the per-venue TRADING-only symbol set with a 10-min
// TTL. Refresh is best-effort: on REST 418 / network failure we keep the
// previous set rather than caching empty (would drop everything).
//
// Filters delisted/halted contracts that linger in funding feeds:
//
//   Aster — SETTLING status (DAM, MATIC, ...)
//   Binance — SETTLING / BREAK / PENDING_TRADING (NTRN, etc.)
//
// Hyperliquid filters via isDelisted in its REST adapter directly.
type listedCache struct {
	mu      sync.RWMutex
	by      map[string]map[string]struct{} // exchange -> set of trading symbols (with USDT suffix)
	updated map[string]time.Time
}

var listed = &listedCache{
	by:      make(map[string]map[string]struct{}),
	updated: make(map[string]time.Time),
}

const listedTTL = 10 * time.Minute

// listedSources — venue → /exchangeInfo URL. Add more as we learn which
// venues lie in their funding feed (Bybit/Gate/MEXC didn't show up in
// the original delisted reports — keeping them un-filtered for now).
var listedSources = map[string]string{
	"binance": "https://fapi.binance.com/fapi/v1/exchangeInfo",
	"aster":   "https://fapi.asterdex.com/fapi/v1/exchangeInfo",
}

// IsListed returns true if symbol is currently TRADING + PERPETUAL on
// the venue, OR if we have no cache for that venue (fail-open). Fail-open
// is critical so a temporary REST failure doesn't blank the screener.
func IsListed(exchange, symbol string) bool {
	src, ok := listedSources[exchange]
	if !ok {
		return true // venue not filtered — pass through
	}

	listed.mu.RLock()
	set, hasSet := listed.by[exchange]
	updated := listed.updated[exchange]
	listed.mu.RUnlock()

	if !hasSet || time.Since(updated) > listedTTL {
		go refreshListed(exchange, src)
	}
	if !hasSet {
		return true // fail-open until first refresh lands
	}
	_, ok = set[symbol+"USDT"]
	return ok
}

func refreshListed(exchange, url string) {
	// Single in-flight per venue.
	listed.mu.Lock()
	if time.Since(listed.updated[exchange]) < 1*time.Second {
		listed.mu.Unlock()
		return
	}
	listed.updated[exchange] = time.Now() // claim the slot to prevent piling up
	listed.mu.Unlock()

	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return
	}
	req.Header.Set("User-Agent", "Mozilla/5.0 avalant-fetcher/go")
	cl := &http.Client{Timeout: 8 * time.Second}
	resp, err := cl.Do(req)
	if err != nil {
		log.L().Debug().Err(err).Str("ex", exchange).Msg("listed refresh failed")
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		log.L().Debug().Int("status", resp.StatusCode).Str("ex", exchange).Msg("listed refresh non-200")
		return
	}
	var doc struct {
		Symbols []struct {
			Symbol       string `json:"symbol"`
			Status       string `json:"status"`
			ContractType string `json:"contractType"`
		} `json:"symbols"`
	}
	if err := sonic.ConfigStd.NewDecoder(resp.Body).Decode(&doc); err != nil {
		return
	}
	if len(doc.Symbols) == 0 {
		return
	}
	out := make(map[string]struct{}, len(doc.Symbols))
	for _, s := range doc.Symbols {
		if s.Status != "TRADING" {
			continue
		}
		if s.ContractType != "" && s.ContractType != "PERPETUAL" {
			continue
		}
		if !strings.HasSuffix(s.Symbol, "USDT") {
			continue
		}
		out[s.Symbol] = struct{}{}
	}
	listed.mu.Lock()
	listed.by[exchange] = out
	listed.updated[exchange] = time.Now()
	listed.mu.Unlock()
	log.L().Info().Str("ex", exchange).Int("trading", len(out)).Msg("listed cache refreshed")
}
