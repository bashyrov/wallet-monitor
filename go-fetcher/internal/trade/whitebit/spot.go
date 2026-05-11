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
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

func toWBSpot(symbol string) string {
	return strings.ToUpper(strings.TrimSpace(symbol)) + "_USDT"
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
	body, err := a.signedRequest(ctx, creds, "/api/v4/order/stock_market",
		map[string]any{
			"market": toWBSpot(req.Symbol),
			"side":   side,
			"amount": qtyString(req.Quantity),
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
	out, err := a.signedRequest(ctx, creds, "/api/v4/order/stock_market",
		map[string]any{
			"market": base + "_USDT",
			"side":   "sell",
			"amount": qtyString(freeBase),
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
