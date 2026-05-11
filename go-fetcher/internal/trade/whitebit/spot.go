// WhiteBIT SPOT extension — same baseURL (whitebit.com), same signed
// envelope (X-TXC-APIKEY + X-TXC-PAYLOAD + X-TXC-SIGNATURE with the
// HMAC-SHA512-hex of the base64 body). Spot endpoint is
// /api/v4/order/market and the symbol form is BTC_USDT (vs futures'
// BTC_PERP).
//
// Implements trade.SpotAdapter.

package whitebit

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

func toWBSpot(symbol string) string {
	return strings.ToUpper(strings.TrimSpace(symbol)) + "_USDT"
}

// wbMarkets caches /api/v4/public/markets for stockPrec lookups.
var (
	wbMarketsMu  sync.RWMutex
	wbMarkets    map[string]int
	wbMarketsAt  time.Time
)

// wbStockPrec returns the stockPrec for `market` (e.g. SOL_USDT). -1 if unknown.
// WhiteBIT rejects sell orders whose amount exceeds this precision with a
// generic "Validation failed" — round DOWN before submit.
func wbStockPrec(ctx context.Context, market string) int {
	wbMarketsMu.RLock()
	if wbMarkets != nil && time.Since(wbMarketsAt) < 30*time.Minute {
		p, ok := wbMarkets[market]
		wbMarketsMu.RUnlock()
		if ok {
			return p
		}
		return -1
	}
	wbMarketsMu.RUnlock()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet,
		"https://whitebit.com/api/v4/public/markets", nil)
	if err != nil {
		return -1
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return -1
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var rows []struct {
		Name      string `json:"name"`
		StockPrec string `json:"stockPrec"`
	}
	if err := json.Unmarshal(raw, &rows); err != nil {
		return -1
	}
	out := make(map[string]int, len(rows))
	for _, r := range rows {
		p, _ := strconv.Atoi(r.StockPrec)
		out[r.Name] = p
	}
	wbMarketsMu.Lock()
	wbMarkets = out
	wbMarketsAt = time.Now()
	wbMarketsMu.Unlock()
	if p, ok := out[market]; ok {
		return p
	}
	return -1
}

func (a *Adapter) PlaceSpotOrder(ctx context.Context, creds trade.Creds, req trade.OpenRequest) (*trade.Result, error) {
	if err := req.Validate(); err != nil {
		return nil, err
	}
	side := "buy"
	if req.Side == trade.SideSell {
		side = "sell"
	}
	// WhiteBit has two spot market endpoints:
	//   /api/v4/order/market        — `amount` in QUOTE currency (USDT)
	//   /api/v4/order/stock_market  — `amount` in BASE currency (SOL)
	// We always carry base-coin qty, so use the latter. Default endpoint
	// silently treats our 0.15 as "spend 0.15 USDT" → fails validation.
	market := toWBSpot(req.Symbol)
	amount := req.Quantity
	prec := wbStockPrec(ctx, market)
	amountStr := qtyString(amount)
	if prec >= 0 {
		factor := 1.0
		for i := 0; i < prec; i++ {
			factor *= 10
		}
		amount = float64(int64(amount*factor)) / factor
		amountStr = strconv.FormatFloat(amount, 'f', prec, 64)
	}
	body, err := a.signedRequest(ctx, creds, "/api/v4/order/stock_market",
		map[string]any{
			"market": market,
			"side":   side,
			"amount": amountStr,
		})
	if err != nil {
		return nil, err
	}
	var resp struct {
		OrderID json.Number `json:"orderId"`
		ID      json.Number `json:"id"`
	}
	_ = json.Unmarshal(body, &resp)
	id := string(resp.OrderID)
	if id == "" {
		id = string(resp.ID)
	}
	return &trade.Result{
		OrderID:   id,
		Symbol:    req.Symbol,
		Side:      req.Side,
		Quantity:  req.Quantity,
		Status:    "NEW",
		CreatedAt: time.Now().UTC(),
		Raw:       body,
	}, nil
}

func (a *Adapter) CloseSpotPosition(ctx context.Context, creds trade.Creds, req trade.CloseRequest) (*trade.Result, error) {
	base := strings.ToUpper(strings.TrimSpace(req.Symbol))
	if base == "" {
		return nil, errUser("symbol required")
	}
	body, err := a.signedRequest(ctx, creds,
		"/api/v4/trade-account/balance",
		map[string]any{"ticker": base})
	if err != nil {
		return nil, err
	}
	// Response with `ticker`: {"available":"0.5","freeze":"0"} (flat)
	// Response without `ticker`: {"BTC":{"available":...}, "ETH":{...}}
	freeBase := 0.0
	var flat struct {
		Available string `json:"available"`
	}
	if err := json.Unmarshal(body, &flat); err == nil && flat.Available != "" {
		freeBase, _ = strconv.ParseFloat(flat.Available, 64)
	}
	if freeBase == 0 {
		var balances map[string]struct {
			Available string `json:"available"`
		}
		if err := json.Unmarshal(body, &balances); err == nil {
			if b, ok := balances[base]; ok {
				freeBase, _ = strconv.ParseFloat(b.Available, 64)
			}
		}
	}
	if freeBase <= 0 {
		return nil, errUser("No %s balance to close on WhiteBIT spot", base)
	}
	// Round DOWN to market's stockPrec — WhiteBIT returns generic
	// "Validation failed" if the amount exceeds the precision.
	market := base + "_USDT"
	prec := wbStockPrec(ctx, market)
	rounded := freeBase
	if prec >= 0 {
		factor := 1.0
		for i := 0; i < prec; i++ {
			factor *= 10
		}
		rounded = float64(int64(freeBase*factor)) / factor
	}
	out, err := a.signedRequest(ctx, creds, "/api/v4/order/stock_market",
		map[string]any{
			"market": market,
			"side":   "sell",
			"amount": strconv.FormatFloat(rounded, 'f', prec, 64),
		})
	if err != nil {
		return nil, err
	}
	var resp struct {
		OrderID json.Number `json:"orderId"`
	}
	_ = json.Unmarshal(out, &resp)
	return &trade.Result{
		OrderID:   string(resp.OrderID),
		Symbol:    req.Symbol,
		Side:      trade.SideSell,
		Quantity:  freeBase,
		Status:    "NEW",
		CreatedAt: time.Now().UTC(),
		Raw:       out,
	}, nil
}
