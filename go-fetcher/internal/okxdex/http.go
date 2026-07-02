package okxdex

import (
	"context"
	"encoding/json"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/bytedance/sonic"
)

// canonicalNameToIndex mirrors the tokens.go map — keeps the HTTP
// handler tolerant of frontend URL params that use DexScreener-
// canonical chain names ("ethereum", "bsc") rather than raw indexes.
// Any numeric string passes through unchanged.
func chainToIndex(v string) string {
	if v == "" {
		return ""
	}
	if _, err := strconv.Atoi(v); err == nil {
		return v
	}
	if idx, ok := canonicalNameToChainIndex[strings.ToLower(v)]; ok {
		return idx
	}
	return ""
}

// MountRoutes registers `/api/screener/dex-price` and
// `/api/screener/dex-candles` on the given mux. Both are read-only,
// no auth (matches the rest of `/api/screener/*` public REST). The
// signed OKX credentials never leave this process — request params
// are sanitised, response is a thin re-emission.
func (s *Service) MountRoutes(mux *http.ServeMux) {
	mux.HandleFunc("/api/screener/dex-price", s.handlePrice)
	mux.HandleFunc("/api/screener/dex-candles", s.handleCandles)
}

// GET /api/screener/dex-price?chain=<index>&address=<0x…>
// → {"price": 0.05517, "ts": 1783030182}
//
// Called by /arb DEX-mode frontend polling every ~2s for the live
// price of the currently-viewed token.
func (s *Service) handlePrice(w http.ResponseWriter, r *http.Request) {
	chain := chainToIndex(strings.TrimSpace(r.URL.Query().Get("chain")))
	addr := strings.ToLower(strings.TrimSpace(r.URL.Query().Get("address")))
	if chain == "" || addr == "" {
		http.Error(w, "chain and address query params required (chain may be canonical name or numeric index)", http.StatusBadRequest)
		return
	}
	if !s.Configured() {
		http.Error(w, "okxdex not configured", http.StatusServiceUnavailable)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	prices, err := s.client.FetchPrices(ctx, []PriceReq{{ChainIndex: chain, TokenContractAddress: addr}})
	if err != nil && len(prices) == 0 {
		http.Error(w, "upstream error: "+err.Error(), http.StatusBadGateway)
		return
	}
	px, ok := prices[PriceKey(chain, addr)]
	if !ok || px <= 0 {
		writeJSON(w, http.StatusOK, map[string]any{"price": nil, "ts": time.Now().Unix()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"price": px, "ts": time.Now().Unix()})
}

// GET /api/screener/dex-candles?chain=<index>&address=<0x…>&bar=1m&limit=200
// → {"candles": [[ts_ms, open, high, low, close, vol_base, vol_quote, confirmed], ...]}
//
// Called on tab activation to seed the chart, then on interval-change.
// bar is one of {1m,5m,15m,30m,1H,4H,1D,1W}; limit is capped at 300.
func (s *Service) handleCandles(w http.ResponseWriter, r *http.Request) {
	chain := chainToIndex(strings.TrimSpace(r.URL.Query().Get("chain")))
	addr := strings.ToLower(strings.TrimSpace(r.URL.Query().Get("address")))
	bar := strings.TrimSpace(r.URL.Query().Get("bar"))
	limitStr := strings.TrimSpace(r.URL.Query().Get("limit"))
	if chain == "" || addr == "" {
		http.Error(w, "chain and address query params required", http.StatusBadRequest)
		return
	}
	if bar == "" {
		bar = "5m"
	}
	if !validBar(bar) {
		http.Error(w, "invalid bar (allowed: 1m,5m,15m,30m,1H,4H,1D,1W)", http.StatusBadRequest)
		return
	}
	limit := 100
	if v, err := strconv.Atoi(limitStr); err == nil && v > 0 {
		if v > 300 {
			v = 300
		}
		limit = v
	}
	if !s.Configured() {
		http.Error(w, "okxdex not configured", http.StatusServiceUnavailable)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 8*time.Second)
	defer cancel()
	path := "/api/v6/dex/market/candles?chainIndex=" + chain + "&tokenContractAddress=" + addr +
		"&bar=" + bar + "&limit=" + strconv.Itoa(limit)
	var rows [][]any
	if err := s.client.do(ctx, "GET", path, nil, &rows); err != nil {
		http.Error(w, "upstream error: "+err.Error(), http.StatusBadGateway)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"candles": rows})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	// sonic Marshal for consistency w/ rest of okxdex; encoding/json
	// would be fine too but pulling sonic here saves an alloc.
	b, err := sonic.Marshal(v)
	if err != nil {
		_ = json.NewEncoder(w).Encode(v)
		return
	}
	_, _ = w.Write(b)
}

var allowedBars = map[string]struct{}{
	"1m": {}, "5m": {}, "15m": {}, "30m": {},
	"1H": {}, "4H": {}, "1D": {}, "1W": {},
}

func validBar(b string) bool {
	_, ok := allowedBars[b]
	return ok
}
