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
	"io"
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

// bitgetSpotPrice fetches current ticker price for BUY-market sizing.
// Public endpoint, no auth.
func (a *Adapter) bitgetSpotPrice(ctx context.Context, symbol string) (float64, error) {
	url := "https://api.bitget.com/api/v2/spot/market/tickers?symbol=" + symbol
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return 0, err
	}
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var env struct {
		Data []struct {
			LastPr string `json:"lastPr"`
		} `json:"data"`
	}
	if err := json.Unmarshal(raw, &env); err != nil {
		return 0, err
	}
	if len(env.Data) == 0 {
		return 0, errUser("no ticker for %s", symbol)
	}
	return parseFloat(env.Data[0].LastPr), nil
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
	// Bitget v2 spot market BUY interprets `size` as QUOTE currency
	// (USDT), SELL interprets as BASE. We always receive base-coin qty,
	// so convert for BUY using a quick public-ticker fetch.
	sizeStr := qtyString(req.Quantity, 8)
	if side == "buy" {
		px, perr := a.bitgetSpotPrice(ctx, sym)
		if perr != nil || px <= 0 {
			return nil, errUser("Bitget spot BUY: could not fetch price for %s: %v", sym, perr)
		}
		// 2-decimal USDT precision is the safe default Bitget accepts.
		sizeStr = strconv.FormatFloat(req.Quantity*px, 'f', 2, 64)
	}
	body, err := a.signedRequest(ctx, creds, http.MethodPost,
		"/api/v2/spot/trade/place-order", nil, map[string]any{
			"symbol":    sym,
			"side":      side,
			"orderType": "market",
			"size":      sizeStr,
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
