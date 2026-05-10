// BingX SPOT extension — same baseURL (open-api.bingx.com), same
// signed-query (HMAC-SHA256 hex), same X-BX-APIKEY header. Spot
// endpoint is /openApi/spot/v1/trade/order with symbol form BTC-USDT
// (dash, vs the dashless BTCUSDT used by other Binance-clones).
//
// Implements trade.SpotAdapter.

package bingx

import (
	"context"
	"encoding/json"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

func toBingxSpot(symbol string) string {
	return strings.ToUpper(strings.TrimSpace(symbol)) + "-USDT"
}

func (a *Adapter) PlaceSpotOrder(ctx context.Context, creds trade.Creds, req trade.OpenRequest) (*trade.Result, error) {
	if err := req.Validate(); err != nil {
		return nil, err
	}
	sym := toBingxSpot(req.Symbol)
	side := "BUY"
	if req.Side == trade.SideSell {
		side = "SELL"
	}
	body, err := a.signedRequest(ctx, creds, http.MethodPost,
		"/openApi/spot/v1/trade/order",
		map[string]string{
			"symbol":   sym,
			"side":     side,
			"type":     "MARKET",
			"quantity": qtyString(req.Quantity, 8),
		})
	if err != nil {
		return nil, err
	}
	var resp struct {
		OrderID  json.Number `json:"orderId"`
		ClientID string      `json:"clientOrderID"`
		Status   string      `json:"status"`
	}
	_ = json.Unmarshal(body, &resp)
	return &trade.Result{
		OrderID:       string(resp.OrderID),
		Symbol:        req.Symbol,
		Side:          req.Side,
		Quantity:      req.Quantity,
		Status:        firstNonEmpty(resp.Status, "NEW"),
		ClientOrderID: resp.ClientID,
		CreatedAt:     time.Now().UTC(),
		Raw:           body,
	}, nil
}

func (a *Adapter) CloseSpotPosition(ctx context.Context, creds trade.Creds, req trade.CloseRequest) (*trade.Result, error) {
	base := strings.ToUpper(strings.TrimSpace(req.Symbol))
	if base == "" {
		return nil, errUser("symbol required")
	}
	body, err := a.signedRequest(ctx, creds, http.MethodGet,
		"/openApi/spot/v1/account/balance", nil)
	if err != nil {
		return nil, err
	}
	var env struct {
		Balances []struct {
			Asset string `json:"asset"`
			Free  string `json:"free"`
		} `json:"balances"`
	}
	_ = json.Unmarshal(body, &env)
	var freeBase float64
	for _, b := range env.Balances {
		if b.Asset == base {
			freeBase, _ = strconv.ParseFloat(b.Free, 64)
			break
		}
	}
	if freeBase <= 0 {
		return nil, errUser("No %s balance to close on BingX spot", base)
	}
	out, err := a.signedRequest(ctx, creds, http.MethodPost,
		"/openApi/spot/v1/trade/order",
		map[string]string{
			"symbol":   base + "-USDT",
			"side":     "SELL",
			"type":     "MARKET",
			"quantity": qtyString(freeBase, 8),
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

func firstNonEmpty(a, b string) string {
	if a != "" {
		return a
	}
	return b
}
