// Bitget V2 SPOT extension — same baseURL (api.bitget.com), same
// signing scheme (HMAC-Base64-SHA256 over ts+method+path+body), same
// passphrase header. Endpoint switches to /api/v2/spot/trade/place-order
// and the symbol form is BTCUSDT (no contract suffix).
//
// Implements trade.SpotAdapter.

package bitget

import (
	"context"
	"encoding/json"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

func parseFloat(s string) float64 {
	v, _ := strconv.ParseFloat(s, 64)
	return v
}

func (a *Adapter) PlaceSpotOrder(ctx context.Context, creds trade.Creds, req trade.OpenRequest) (*trade.Result, error) {
	if err := req.Validate(); err != nil {
		return nil, err
	}
	sym := strings.ToUpper(strings.TrimSpace(req.Symbol)) + "USDT"
	side := "buy"
	if req.Side == trade.SideSell {
		side = "sell"
	}
	body, err := a.signedRequest(ctx, creds, http.MethodPost,
		"/api/v2/spot/trade/place-order", nil, map[string]any{
			"symbol":    sym,
			"side":      side,
			"orderType": "market",
			"size":      qtyString(req.Quantity, 8),
			"force":     "ioc",
		})
	if err != nil {
		return nil, err
	}
	var resp struct {
		OrderID  string `json:"orderId"`
		ClientID string `json:"clientOid"`
	}
	_ = json.Unmarshal(body, &resp)
	return &trade.Result{
		OrderID:       resp.OrderID,
		Symbol:        req.Symbol,
		Side:          req.Side,
		Quantity:      req.Quantity,
		Status:        "NEW",
		ClientOrderID: resp.ClientID,
		CreatedAt:     time.Now().UTC(),
		Raw:           body,
	}, nil
}

// CloseSpotPosition — sell entire base balance via /api/v2/spot/account/assets.
func (a *Adapter) CloseSpotPosition(ctx context.Context, creds trade.Creds, req trade.CloseRequest) (*trade.Result, error) {
	base := strings.ToUpper(strings.TrimSpace(req.Symbol))
	if base == "" {
		return nil, errUser("symbol required")
	}
	body, err := a.signedRequest(ctx, creds, http.MethodGet,
		"/api/v2/spot/account/assets",
		map[string]string{"coin": base}, nil)
	if err != nil {
		return nil, err
	}
	var rows []struct {
		Coin      string `json:"coin"`
		Available string `json:"available"`
	}
	_ = json.Unmarshal(body, &rows)
	var freeBase float64
	for _, r := range rows {
		if r.Coin == base {
			freeBase = parseFloat(r.Available)
			break
		}
	}
	if freeBase <= 0 {
		return nil, errUser("No %s balance to close on Bitget spot", base)
	}
	out, err := a.signedRequest(ctx, creds, http.MethodPost,
		"/api/v2/spot/trade/place-order", nil, map[string]any{
			"symbol":    base + "USDT",
			"side":      "sell",
			"orderType": "market",
			"size":      qtyString(freeBase, 8),
			"force":     "ioc",
		})
	if err != nil {
		return nil, err
	}
	var resp struct {
		OrderID string `json:"orderId"`
	}
	_ = json.Unmarshal(out, &resp)
	return &trade.Result{
		OrderID:   resp.OrderID,
		Symbol:    req.Symbol,
		Side:      trade.SideSell,
		Quantity:  freeBase,
		Status:    "NEW",
		CreatedAt: time.Now().UTC(),
		Raw:       out,
	}, nil
}
