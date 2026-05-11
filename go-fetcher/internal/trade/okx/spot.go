// OKX SPOT extension — same baseURL (www.okx.com), same V5 envelope,
// same /api/v5/trade/order endpoint. The differences from SWAP:
//   - instId form: BTC-USDT (no -SWAP suffix)
//   - tdMode: "cash" (spot has no margin)
//   - no posSide field
//   - sz is in BASE units, not contracts (no ÷ ctVal conversion)
//
// Implements trade.SpotAdapter.

package okx

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

func toOKXSpot(symbol string) string {
	return strings.ToUpper(strings.TrimSpace(symbol)) + "-USDT"
}

func (a *Adapter) PlaceSpotOrder(ctx context.Context, creds trade.Creds, req trade.OpenRequest) (*trade.Result, error) {
	if err := req.Validate(); err != nil {
		return nil, err
	}
	instID := toOKXSpot(req.Symbol)
	side := "buy"
	if req.Side == trade.SideSell {
		side = "sell"
	}
	// OKX spot market BUY defaults `sz` to QUOTE currency (USDT). Force
	// tgtCcy=base_ccy so we can always pass base-coin quantity.
	body, err := a.signedRequest(ctx, creds, http.MethodPost, "/api/v5/trade/order",
		map[string]any{
			"instId":  instID,
			"tdMode":  "cash",
			"side":    side,
			"ordType": "market",
			"sz":      qtyString(req.Quantity),
			"tgtCcy":  "base_ccy",
		})
	if err != nil {
		return nil, err
	}
	var rows []struct {
		OrdID   string `json:"ordId"`
		ClOrdID string `json:"clOrdId"`
		SCode   string `json:"sCode"`
		SMsg    string `json:"sMsg"`
	}
	_ = json.Unmarshal(body, &rows)
	orderID, clOrdID := "", ""
	if len(rows) > 0 {
		orderID = rows[0].OrdID
		clOrdID = rows[0].ClOrdID
	}
	return &trade.Result{
		OrderID:       orderID,
		Symbol:        req.Symbol,
		Side:          req.Side,
		Quantity:      req.Quantity,
		Status:        "NEW",
		ClientOrderID: clOrdID,
		CreatedAt:     time.Now().UTC(),
		Raw:           body,
	}, nil
}

// CloseSpotPosition — fetch /api/v5/account/balance, sell free balance
// of the base asset for USDT.
func (a *Adapter) CloseSpotPosition(ctx context.Context, creds trade.Creds, req trade.CloseRequest) (*trade.Result, error) {
	base := strings.ToUpper(strings.TrimSpace(req.Symbol))
	if base == "" {
		return nil, errUser("symbol required")
	}
	body, err := a.signedRequest(ctx, creds, http.MethodGet,
		"/api/v5/account/balance?ccy="+base, nil)
	if err != nil {
		return nil, err
	}
	var env []struct {
		Details []struct {
			Ccy     string `json:"ccy"`
			AvailEq string `json:"availEq"`
			AvailBal string `json:"availBal"`
		} `json:"details"`
	}
	_ = json.Unmarshal(body, &env)
	var freeBase float64
	if len(env) > 0 {
		for _, d := range env[0].Details {
			if d.Ccy == base {
				freeBase = parseFloat(d.AvailEq)
				if freeBase == 0 {
					freeBase = parseFloat(d.AvailBal)
				}
				break
			}
		}
	}
	if freeBase <= 0 {
		return nil, errUser("No %s balance to close on OKX spot", base)
	}
	out, err := a.signedRequest(ctx, creds, http.MethodPost, "/api/v5/trade/order",
		map[string]any{
			"instId":  base + "-USDT",
			"tdMode":  "cash",
			"side":    "sell",
			"ordType": "market",
			"sz":      qtyString(freeBase),
			"tgtCcy":  "base_ccy",
		})
	if err != nil {
		return nil, err
	}
	var rows []struct {
		OrdID string `json:"ordId"`
	}
	_ = json.Unmarshal(out, &rows)
	orderID := ""
	if len(rows) > 0 {
		orderID = rows[0].OrdID
	}
	return &trade.Result{
		OrderID:   orderID,
		Symbol:    req.Symbol,
		Side:      trade.SideSell,
		Quantity:  freeBase,
		Status:    "NEW",
		CreatedAt: time.Now().UTC(),
		Raw:       out,
	}, nil
}
