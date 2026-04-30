package binance

import (
	"context"
	"net/http"
	"sync"
	"time"

	"github.com/bytedance/sonic"
)

// Bug #8 — Binance keeps delisted/halted symbols in /fapi/v1/premiumIndex
// and /api/v3/ticker/24hr for days (status=BREAK / SETTLING). NTRN is the
// canonical canary. Cross-checking against /exchangeInfo's status==TRADING
// set filters them. 10-minute cache mirrors Python.

type tradingFilter struct {
	mu        sync.RWMutex
	symbols   map[string]struct{}
	updatedAt time.Time
	endpoint  string
}

const tradingCacheTTL = 10 * time.Minute

// NewFuturesTradingFilter — caches /fapi/v1/exchangeInfo TRADING-only
// symbols. Adapters should consult IsTrading() on parsing every snapshot
// from the firehose channels (premiumIndex, ticker24hr).
func NewFuturesTradingFilter() *tradingFilter {
	return &tradingFilter{endpoint: "https://fapi.binance.com/fapi/v1/exchangeInfo"}
}

// NewSpotTradingFilter — same, but spot endpoint.
func NewSpotTradingFilter() *tradingFilter {
	return &tradingFilter{endpoint: "https://api.binance.com/api/v3/exchangeInfo"}
}

func (f *tradingFilter) IsTrading(ctx context.Context, symbol string) bool {
	f.mu.RLock()
	fresh := time.Since(f.updatedAt) < tradingCacheTTL && f.symbols != nil
	if fresh {
		_, ok := f.symbols[symbol]
		f.mu.RUnlock()
		return ok
	}
	f.mu.RUnlock()

	if err := f.refresh(ctx); err != nil {
		// Don't filter on transient REST errors — better to let a delisted
		// symbol through for one cycle than to block all symbols.
		return true
	}
	f.mu.RLock()
	_, ok := f.symbols[symbol]
	f.mu.RUnlock()
	return ok
}

type symbolInfo struct {
	Symbol             string `json:"symbol"`
	Status             string `json:"status"`
	ContractType       string `json:"contractType,omitempty"`
	IsSpotTradingAllow bool   `json:"isSpotTradingAllowed,omitempty"`
}
type exchangeInfo struct {
	Symbols []symbolInfo `json:"symbols"`
}

func (f *tradingFilter) refresh(ctx context.Context) error {
	req, err := http.NewRequestWithContext(ctx, "GET", f.endpoint, nil)
	if err != nil {
		return err
	}
	req.Header.Set("User-Agent", "Mozilla/5.0 avalant-fetcher/go")

	cl := &http.Client{Timeout: 8 * time.Second}
	resp, err := cl.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	var info exchangeInfo
	dec := sonic.ConfigStd.NewDecoder(resp.Body)
	if err := dec.Decode(&info); err != nil {
		return err
	}

	out := make(map[string]struct{}, len(info.Symbols))
	for _, s := range info.Symbols {
		if s.Status != "TRADING" {
			continue
		}
		// Futures-specific: skip non-PERPETUAL contracts (delivery, expired).
		if s.ContractType != "" && s.ContractType != "PERPETUAL" {
			continue
		}
		out[s.Symbol] = struct{}{}
	}
	f.mu.Lock()
	f.symbols = out
	f.updatedAt = time.Now()
	f.mu.Unlock()
	return nil
}
