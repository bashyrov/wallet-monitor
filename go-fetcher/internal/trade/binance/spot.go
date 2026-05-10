// Binance SPOT (api.binance.com) extension to the futures adapter.
//
// Same Adapter struct, same HMAC-SHA256 signing, same `X-MBX-APIKEY`
// header — only the base URL + endpoint paths differ. Spot is always
// 1× / cash, so we skip the futures-specific exchangeInfo lookup,
// hedge-mode dance, and leverage/margin preflight: a single signed
// POST and we're done.
//
// Implements trade.SpotAdapter.

package binance

import (
	"context"
	"encoding/json"
	"io"
	"math"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

const spotBaseURL = "https://api.binance.com"

// Spot exchangeInfo cache — per-symbol LOT_SIZE filter for quantity
// rounding. Refreshed lazily every 10 min, same TTL as the futures
// equivalent.
var (
	spotInfoMu     sync.RWMutex
	spotInfo       map[string]symbolInfo
	spotInfoLoaded time.Time
)

func (a *Adapter) spotExchangeInfo(ctx context.Context) (map[string]symbolInfo, error) {
	spotInfoMu.RLock()
	if spotInfo != nil && time.Since(spotInfoLoaded) < infoTTL {
		out := spotInfo
		spotInfoMu.RUnlock()
		return out, nil
	}
	spotInfoMu.RUnlock()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, spotBaseURL+"/api/v3/exchangeInfo", nil)
	if err != nil {
		return nil, err
	}
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, mapNetErr(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return nil, parseExchangeError(resp.StatusCode, body)
	}
	var payload struct {
		Symbols []struct {
			Symbol            string `json:"symbol"`
			QuoteAsset        string `json:"quoteAsset"`
			Status            string `json:"status"`
			IsSpotTradingAllowed bool `json:"isSpotTradingAllowed"`
			BaseAssetPrecision int    `json:"baseAssetPrecision"`
			Filters           []struct {
				FilterType  string `json:"filterType"`
				StepSize    string `json:"stepSize,omitempty"`
				MinQty      string `json:"minQty,omitempty"`
				MinNotional string `json:"minNotional,omitempty"`
				TickSize    string `json:"tickSize,omitempty"`
			} `json:"filters"`
		} `json:"symbols"`
	}
	if err := json.Unmarshal(body, &payload); err != nil {
		return nil, errInternal("parse spot exchangeInfo", err)
	}
	out := make(map[string]symbolInfo, len(payload.Symbols))
	for _, s := range payload.Symbols {
		if !s.IsSpotTradingAllowed || s.Status != "TRADING" {
			continue
		}
		if s.QuoteAsset != "USDT" {
			continue
		}
		info := symbolInfo{QuantityPrecision: s.BaseAssetPrecision}
		for _, f := range s.Filters {
			switch f.FilterType {
			case "LOT_SIZE":
				info.StepSize = parseFloat(f.StepSize)
				info.MinQty = parseFloat(f.MinQty)
			case "NOTIONAL", "MIN_NOTIONAL":
				info.MinNotional = parseFloat(f.MinNotional)
			case "PRICE_FILTER":
				info.TickSize = parseFloat(f.TickSize)
			}
		}
		out[s.Symbol] = info
	}
	spotInfoMu.Lock()
	spotInfo = out
	spotInfoLoaded = time.Now()
	spotInfoMu.Unlock()
	return out, nil
}

func (a *Adapter) signedSpotRequest(
	ctx context.Context, creds trade.Creds, method, path string,
	params map[string]string,
) (json.RawMessage, error) {
	if params == nil {
		params = map[string]string{}
	}
	params["timestamp"] = strconv.FormatInt(time.Now().UnixMilli(), 10)
	if _, ok := params["recvWindow"]; !ok {
		params["recvWindow"] = "5000"
	}
	q := trade.SortedFormQuery(params)
	sig := trade.HMACHexSHA256(creds.APISecret, q)
	full := q + "&signature=" + sig

	var req *http.Request
	var err error
	switch method {
	case http.MethodGet, http.MethodDelete:
		req, err = http.NewRequestWithContext(ctx, method, spotBaseURL+path+"?"+full, nil)
	case http.MethodPost:
		req, err = http.NewRequestWithContext(ctx, method, spotBaseURL+path, strings.NewReader(full))
		if req != nil {
			req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
		}
	default:
		return nil, errUser("unsupported method %s", method)
	}
	if err != nil {
		return nil, err
	}
	req.Header.Set("X-MBX-APIKEY", creds.APIKey)
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, mapNetErr(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return nil, parseExchangeError(resp.StatusCode, body)
	}
	return json.RawMessage(body), nil
}

// PlaceSpotOrder — single signed POST to /api/v3/order, type=MARKET.
// BUY uses `quoteOrderQty` (USDT amount) for predictable spend; SELL
// uses `quantity` (base asset, derived from current spot wallet).
//
// For arb-style "buy spot, short perp", caller passes Quantity in
// BASE units (consistent with futures contract). We support both:
// if Quantity * spot_price would exceed the user's USDT balance the
// venue 4xx's; we don't preflight (skipped for speed).
func (a *Adapter) PlaceSpotOrder(ctx context.Context, creds trade.Creds, req trade.OpenRequest) (*trade.Result, error) {
	if err := req.Validate(); err != nil {
		return nil, err
	}
	sym := toBinanceSymbol(req.Symbol) // BTC → BTCUSDT
	infoMap, _ := a.spotExchangeInfo(ctx)
	info := infoMap[sym]
	qty := roundToStep(req.Quantity, info.StepSize, info.QuantityPrecision)
	if qty <= 0 {
		return nil, errUser("Quantity rounds to zero against %s lot size", sym)
	}
	if info.MinQty > 0 && qty < info.MinQty {
		return nil, errUser("Quantity below minimum (%g %s)", info.MinQty, req.Symbol)
	}
	params := map[string]string{
		"symbol":   sym,
		"side":     binanceSide(req.Side),
		"type":     "MARKET",
		"quantity": qtyString(qty, info.QuantityPrecision),
	}
	body, err := a.signedSpotRequest(ctx, creds, http.MethodPost, "/api/v3/order", params)
	if err != nil {
		return nil, err
	}
	var resp struct {
		OrderID         json.Number `json:"orderId"`
		ExecutedQty     string      `json:"executedQty"`
		CummulativeQuoteQty string  `json:"cummulativeQuoteQty"`
		Status          string      `json:"status"`
		ClientID        string      `json:"clientOrderId"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		return nil, errInternal("parse spot order response", err)
	}
	executed := parseFloat(resp.ExecutedQty)
	avg := 0.0
	if executed > 0 {
		avg = parseFloat(resp.CummulativeQuoteQty) / executed
	}
	return &trade.Result{
		OrderID:       string(resp.OrderID),
		Symbol:        req.Symbol,
		Side:          req.Side,
		Quantity:      executed,
		AvgPrice:      avg,
		Status:        resp.Status,
		ClientOrderID: resp.ClientID,
		CreatedAt:     time.Now().UTC(),
		Raw:           body,
	}, nil
}

// CloseSpotPosition — sell entire base-asset balance for the symbol.
// Spot has no concept of "close" (no leverage, no margin). For an
// arb-pair close, we sell whatever's currently in the wallet for the
// base asset.
func (a *Adapter) CloseSpotPosition(ctx context.Context, creds trade.Creds, req trade.CloseRequest) (*trade.Result, error) {
	base := strings.ToUpper(strings.TrimSpace(req.Symbol))
	if base == "" {
		return nil, errUser("symbol required")
	}
	// Fetch wallet balance for the base asset.
	body, err := a.signedSpotRequest(ctx, creds, http.MethodGet, "/api/v3/account", nil)
	if err != nil {
		return nil, err
	}
	var acct struct {
		Balances []struct {
			Asset  string `json:"asset"`
			Free   string `json:"free"`
		} `json:"balances"`
	}
	if err := json.Unmarshal(body, &acct); err != nil {
		return nil, errInternal("parse account", err)
	}
	var freeBase float64
	for _, b := range acct.Balances {
		if b.Asset == base {
			freeBase = parseFloat(b.Free)
			break
		}
	}
	if freeBase <= 0 {
		return nil, errUser("No %s balance to close on Binance spot", base)
	}
	infoMap, _ := a.spotExchangeInfo(ctx)
	info := infoMap[base+"USDT"]
	qty := roundToStep(freeBase, info.StepSize, info.QuantityPrecision)
	if qty <= 0 || (info.MinQty > 0 && qty < info.MinQty) {
		return nil, errUser("Free %s balance (%g) below min lot", base, freeBase)
	}
	params := map[string]string{
		"symbol":   base + "USDT",
		"side":     "SELL",
		"type":     "MARKET",
		"quantity": qtyString(qty, info.QuantityPrecision),
	}
	out, err := a.signedSpotRequest(ctx, creds, http.MethodPost, "/api/v3/order", params)
	if err != nil {
		return nil, err
	}
	var resp struct {
		OrderID             json.Number `json:"orderId"`
		ExecutedQty         string      `json:"executedQty"`
		CummulativeQuoteQty string      `json:"cummulativeQuoteQty"`
		Status              string      `json:"status"`
	}
	if err := json.Unmarshal(out, &resp); err != nil {
		return nil, errInternal("parse spot close response", err)
	}
	executed := parseFloat(resp.ExecutedQty)
	avg := 0.0
	if executed > 0 {
		avg = math.Round(parseFloat(resp.CummulativeQuoteQty)/executed*100000) / 100000
	}
	return &trade.Result{
		OrderID:   string(resp.OrderID),
		Symbol:    req.Symbol,
		Side:      trade.SideSell,
		Quantity:  executed,
		AvgPrice:  avg,
		Status:    resp.Status,
		CreatedAt: time.Now().UTC(),
		Raw:       out,
	}, nil
}
