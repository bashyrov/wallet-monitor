// Aster /fapi/v1/exchangeInfo trading-filter — same shape as Binance's
// (Aster is a Binance fork). Used by BuildSubscribe to avoid asking
// the WS to subscribe to symbols Aster doesn't list (which would fail
// the entire frame with 1008 policy violation).
package aster

import (
	"context"
	"errors"
	"net/http"
	"sync"
	"time"

	"github.com/bytedance/sonic"
)

const tradingCacheTTL = 10 * time.Minute

type tradingFilter struct {
	mu        sync.RWMutex
	symbols   map[string]struct{}
	updatedAt time.Time
}

func newTradingFilter() *tradingFilter { return &tradingFilter{} }

func (f *tradingFilter) IsTrading(ctx context.Context, symbol string) bool {
	f.mu.RLock()
	if time.Since(f.updatedAt) < tradingCacheTTL && f.symbols != nil {
		_, ok := f.symbols[symbol]
		f.mu.RUnlock()
		return ok
	}
	f.mu.RUnlock()
	if err := f.refresh(ctx); err != nil {
		return true // fail-open on transient REST error
	}
	f.mu.RLock()
	_, ok := f.symbols[symbol]
	f.mu.RUnlock()
	return ok
}

func (f *tradingFilter) refresh(ctx context.Context) error {
	req, err := http.NewRequestWithContext(ctx, "GET",
		"https://fapi.asterdex.com/fapi/v1/exchangeInfo", nil)
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
	if resp.StatusCode != 200 {
		return errors.New("aster exchangeInfo http " + resp.Status)
	}
	var doc struct {
		Symbols []struct {
			Symbol       string `json:"symbol"`
			Status       string `json:"status"`
			ContractType string `json:"contractType,omitempty"`
		} `json:"symbols"`
	}
	if err := sonic.ConfigStd.NewDecoder(resp.Body).Decode(&doc); err != nil {
		return err
	}
	if len(doc.Symbols) == 0 {
		return errors.New("aster exchangeInfo empty")
	}
	out := make(map[string]struct{}, len(doc.Symbols))
	for _, s := range doc.Symbols {
		if s.Status != "TRADING" {
			continue
		}
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
