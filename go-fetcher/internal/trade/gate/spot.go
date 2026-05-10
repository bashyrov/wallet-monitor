// Gate.io SPOT extension — same signed envelope (HMAC-SHA512 hex with
// the gate "KEY/SIGN/Timestamp" headers), same baseURL, but the
// endpoint is /api/v4/spot/orders and the symbol form is BTC_USDT
// (already what toGateSymbol returns) without contract semantics.
//
// Implements trade.SpotAdapter.

package gate

import (
	"context"
	"encoding/json"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

func spotParseFloat(s string) float64 {
	v, _ := strconv.ParseFloat(s, 64)
	return v
}

func (a *Adapter) PlaceSpotOrder(ctx context.Context, creds trade.Creds, req trade.OpenRequest) (*trade.Result, error) {
	if err := req.Validate(); err != nil {
		return nil, err
	}
	pair := toGateSymbol(req.Symbol)
	side := "buy"
	if req.Side == trade.SideSell {
		side = "sell"
	}
	body, err := a.signedRequest(ctx, creds, http.MethodPost, "/api/v4/spot/orders",
		nil, map[string]any{
			"currency_pair": pair,
			"type":          "market",
			"side":          side,
			"amount":        formatAmount(req.Quantity),
			"time_in_force": "ioc",
		})
	if err != nil {
		return nil, err
	}
	var resp struct {
		ID         string `json:"id"`
		Status     string `json:"status"`
		FilledTotal string `json:"filled_total"`
		Amount     string `json:"amount"`
		AvgDealPrice string `json:"avg_deal_price"`
	}
	_ = json.Unmarshal(body, &resp)
	executed := spotParseFloat(resp.Amount)
	avg := spotParseFloat(resp.AvgDealPrice)
	return &trade.Result{
		OrderID:   resp.ID,
		Symbol:    req.Symbol,
		Side:      req.Side,
		Quantity:  executed,
		AvgPrice:  avg,
		Status:    resp.Status,
		CreatedAt: time.Now().UTC(),
		Raw:       body,
	}, nil
}

// CloseSpotPosition — sell entire base-asset balance via /api/v4/spot/accounts.
func (a *Adapter) CloseSpotPosition(ctx context.Context, creds trade.Creds, req trade.CloseRequest) (*trade.Result, error) {
	base := strings.ToUpper(strings.TrimSpace(req.Symbol))
	if base == "" {
		return nil, errUser("symbol required")
	}
	body, err := a.signedRequest(ctx, creds, http.MethodGet,
		"/api/v4/spot/accounts",
		map[string]string{"currency": base}, nil)
	if err != nil {
		return nil, err
	}
	var rows []struct {
		Currency  string `json:"currency"`
		Available string `json:"available"`
	}
	_ = json.Unmarshal(body, &rows)
	var freeBase float64
	for _, r := range rows {
		if r.Currency == base {
			freeBase = spotParseFloat(r.Available)
			break
		}
	}
	if freeBase <= 0 {
		return nil, errUser("No %s balance to close on Gate spot", base)
	}
	out, err := a.signedRequest(ctx, creds, http.MethodPost, "/api/v4/spot/orders",
		nil, map[string]any{
			"currency_pair": base + "_USDT",
			"type":          "market",
			"side":          "sell",
			"amount":        formatAmount(freeBase),
			"time_in_force": "ioc",
		})
	if err != nil {
		return nil, err
	}
	var resp struct {
		ID     string `json:"id"`
		Status string `json:"status"`
	}
	_ = json.Unmarshal(out, &resp)
	return &trade.Result{
		OrderID:   resp.ID,
		Symbol:    req.Symbol,
		Side:      trade.SideSell,
		Quantity:  freeBase,
		Status:    resp.Status,
		CreatedAt: time.Now().UTC(),
		Raw:       out,
	}, nil
}

// formatAmount — Gate accepts up to 8 decimal places; trim trailing zeros.
func formatAmount(qty float64) string {
	s := strconv.FormatFloat(qty, 'f', 8, 64)
	s = strings.TrimRight(strings.TrimRight(s, "0"), ".")
	if s == "" {
		s = "0"
	}
	return s
}
