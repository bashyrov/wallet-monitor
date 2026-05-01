// Package lighter — Lighter zkPerp DEX.
//
// QUIRK (Bug #16): Lighter subscribes by integer market_id, not symbol.
// We resolve symbol→id from REST /api/v1/orderBooks once at startup and
// re-fetch every hour (markets only change on listings).
package lighter

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/bytedance/sonic"
)

const orderBooksEndpoint = "https://mainnet.zklighter.elliot.ai/api/v1/orderBooks"
const idCacheTTL = 1 * time.Hour

type idMap struct {
	mu      sync.RWMutex
	bySymb  map[string]int
	byID    map[int]string
	updated time.Time
}

func newIDMap() *idMap {
	return &idMap{bySymb: make(map[string]int), byID: make(map[int]string)}
}

func (m *idMap) Resolve(ctx context.Context, symbol string) (int, error) {
	sym := strings.ToUpper(symbol)
	m.mu.RLock()
	if time.Since(m.updated) < idCacheTTL && len(m.bySymb) > 0 {
		id, ok := m.bySymb[sym]
		m.mu.RUnlock()
		if ok {
			return id, nil
		}
		return 0, errors.New("symbol not on lighter")
	}
	m.mu.RUnlock()
	if err := m.refresh(ctx); err != nil {
		return 0, err
	}
	m.mu.RLock()
	defer m.mu.RUnlock()
	id, ok := m.bySymb[sym]
	if !ok {
		return 0, errors.New("symbol not on lighter")
	}
	return id, nil
}

func (m *idMap) Symbol(id int) string {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.byID[id]
}

func (m *idMap) refresh(ctx context.Context) error {
	req, err := http.NewRequestWithContext(ctx, "GET", orderBooksEndpoint, nil)
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
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return err
	}
	var doc struct {
		OrderBooks []struct {
			Symbol     string `json:"symbol"`
			MarketID   int    `json:"market_id"`
			MarketType string `json:"market_type"`
			Status     string `json:"status"`
		} `json:"order_books"`
	}
	if err := sonic.Unmarshal(body, &doc); err != nil {
		return err
	}
	bySymb := make(map[string]int, len(doc.OrderBooks))
	byID := make(map[int]string, len(doc.OrderBooks))
	for _, b := range doc.OrderBooks {
		if !strings.EqualFold(b.MarketType, "perp") {
			continue
		}
		if !strings.EqualFold(b.Status, "active") {
			continue
		}
		sym := strings.ToUpper(b.Symbol)
		if sym == "" {
			continue
		}
		bySymb[sym] = b.MarketID
		byID[b.MarketID] = sym
	}
	m.mu.Lock()
	m.bySymb = bySymb
	m.byID = byID
	m.updated = time.Now()
	m.mu.Unlock()
	return nil
}
