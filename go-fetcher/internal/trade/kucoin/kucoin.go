// KuCoin Futures (USDT-M) trade adapter.
//
// Port of `backend/services/trade_adapters/kucoin.py`.
//
// Signing: base64( HMAC_SHA256(secret, ts + method + url-path-with-query + body) ).
// Passphrase is signed too: base64( HMAC_SHA256(secret, passphrase) ) and
// sent in `KC-API-PASSPHRASE`. KuCoin enforces this via `KC-API-KEY-VERSION: 2`.
//
// Quirks:
//   - Symbol form: "BTCUSDTM" (BTC mapped to XBT, then suffix). E.g.
//     BTC → XBTUSDTM, ETH → ETHUSDTM.
//   - Quantity is in CONTRACTS (lots). qty_coins / multiplier = contracts.
//   - Margin mode field on order body: "ISOLATED" | "CROSS".
//   - Close uses `closeOrder: true` flag — server flattens regardless
//     of size; we still send `size: 1` to satisfy the schema.
package kucoin

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

const baseURL = "https://api-futures.kucoin.com"

type Adapter struct {
	httpClient *http.Client

	infoMu sync.RWMutex
	info   map[string]instrumentInfo
}

type instrumentInfo struct {
	Multiplier  float64
	LotSize     int64
	MaxLeverage int
	At          time.Time
}

const infoTTL = 10 * time.Minute

func New() *Adapter {
	return &Adapter{
		httpClient: &http.Client{
			Timeout: 15 * time.Second,
			Transport: &http.Transport{
				ForceAttemptHTTP2:   true,
				MaxIdleConns:        200,
				MaxIdleConnsPerHost: 32,
				MaxConnsPerHost:     64,
				IdleConnTimeout:     300 * time.Second,
				TLSHandshakeTimeout: 5 * time.Second,
			},
		},
		info: make(map[string]instrumentInfo, 256),
	}
}

func init() { trade.Register("kucoin", New()) }

func (a *Adapter) Name() string { return "kucoin" }

// ── Symbol mapping ───────────────────────────────────────────────────────

func toKucoinSymbol(sym string) string {
	base := strings.ToUpper(sym)
	if base == "BTC" {
		base = "XBT"
	}
	return base + "USDTM"
}

// ── Signing ──────────────────────────────────────────────────────────────

func (a *Adapter) signedRequest(
	ctx context.Context, creds trade.Creds, method, path string,
	params map[string]string, body any,
) (json.RawMessage, error) {
	if creds.Passphrase == "" {
		return nil, errUser("KuCoin requires passphrase credential")
	}
	ts := strconv.FormatInt(time.Now().UnixMilli(), 10)

	urlPath := path
	bodyStr := ""
	if method == http.MethodGet && len(params) > 0 {
		urlPath = path + "?" + trade.SortedFormQuery(params)
	} else if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return nil, errInternal("marshal body", err)
		}
		bodyStr = string(b)
	}

	sig := trade.HMACBase64SHA256(creds.APISecret, ts+method+urlPath+bodyStr)
	passSig := trade.HMACBase64SHA256(creds.APISecret, creds.Passphrase)

	url := baseURL + urlPath
	if method != http.MethodGet {
		url = baseURL + path
	}
	var bodyReader io.Reader
	if method != http.MethodGet {
		if bodyStr == "" {
			bodyStr = "{}"
		}
		bodyReader = strings.NewReader(bodyStr)
	}
	req, err := http.NewRequestWithContext(ctx, method, url, bodyReader)
	if err != nil {
		return nil, err
	}
	req.Header.Set("KC-API-KEY", creds.APIKey)
	req.Header.Set("KC-API-SIGN", sig)
	req.Header.Set("KC-API-TIMESTAMP", ts)
	req.Header.Set("KC-API-PASSPHRASE", passSig)
	req.Header.Set("KC-API-KEY-VERSION", "2")
	req.Header.Set("Content-Type", "application/json")

	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, &trade.Error{Kind: trade.KindTransient, Message: err.Error(), Cause: err}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return nil, parseError(resp.StatusCode, raw)
	}
	var env struct {
		Code string          `json:"code"`
		Msg  string          `json:"msg"`
		Data json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(raw, &env); err != nil {
		return nil, errInternal("parse envelope", err)
	}
	if env.Code != "200000" && env.Code != "" {
		return nil, &trade.Error{Kind: trade.KindExchange, Code: env.Code, Message: friendly(env.Code, env.Msg)}
	}
	return env.Data, nil
}

func parseError(status int, body []byte) *trade.Error {
	var env struct {
		Code string `json:"code"`
		Msg  string `json:"msg"`
	}
	_ = json.Unmarshal(body, &env)
	if status == 429 {
		return &trade.Error{Kind: trade.KindRateLimit, Code: env.Code, Message: friendly(env.Code, env.Msg)}
	}
	return &trade.Error{Kind: trade.KindExchange, Code: env.Code, Message: friendly(env.Code, env.Msg)}
}

// ── Instrument cache ─────────────────────────────────────────────────────

func (a *Adapter) instrumentInfo(ctx context.Context, sym string) (instrumentInfo, error) {
	a.infoMu.RLock()
	hit, ok := a.info[sym]
	a.infoMu.RUnlock()
	if ok && time.Since(hit.At) < infoTTL {
		return hit, nil
	}
	url := baseURL + "/api/v1/contracts/" + sym
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	resp, err := a.httpClient.Do(req)
	if err != nil {
		if ok {
			return hit, nil
		}
		return instrumentInfo{}, &trade.Error{Kind: trade.KindTransient, Message: err.Error()}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var env struct {
		Code string `json:"code"`
		Data struct {
			Multiplier      json.Number `json:"multiplier"`
			LotSize         json.Number `json:"lotSize"`
			MaxLeverage     json.Number `json:"maxLeverage"`
		} `json:"data"`
	}
	if err := json.Unmarshal(raw, &env); err != nil || env.Code != "200000" {
		return instrumentInfo{}, &trade.Error{
			Kind:    trade.KindUser,
			Message: fmt.Sprintf("symbol %s not listed on KuCoin Futures", sym),
		}
	}
	mult, _ := env.Data.Multiplier.Float64()
	lot, _ := env.Data.LotSize.Int64()
	mlv, _ := env.Data.MaxLeverage.Int64()
	if mult <= 0 {
		mult = 1
	}
	if lot <= 0 {
		lot = 1
	}
	out := instrumentInfo{Multiplier: mult, LotSize: lot, MaxLeverage: int(mlv), At: time.Now()}
	a.infoMu.Lock()
	a.info[sym] = out
	a.infoMu.Unlock()
	return out, nil
}

// ── Quantity helpers ─────────────────────────────────────────────────────

func coinsToContracts(qty, multiplier float64, lotSize int64) int64 {
	if multiplier <= 0 {
		multiplier = 1
	}
	n := int64(math.Floor(qty / multiplier))
	if lotSize > 1 {
		n = (n / lotSize) * lotSize
	}
	if n < 0 {
		n = 0
	}
	return n
}

// uuid generates a v4 UUID for the clientOid required on close-orders.
func uuid() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	const hex = "0123456789abcdef"
	out := make([]byte, 36)
	pos := 0
	for i, x := range b {
		out[pos] = hex[x>>4]
		out[pos+1] = hex[x&0x0f]
		pos += 2
		if i == 3 || i == 5 || i == 7 || i == 9 {
			out[pos] = '-'
			pos++
		}
	}
	return string(out)
}

// ── Adapter methods ──────────────────────────────────────────────────────

func (a *Adapter) GetBalance(ctx context.Context, creds trade.Creds) (*trade.Balance, error) {
	body, err := a.signedRequest(ctx, creds, http.MethodGet,
		"/api/v1/account-overview", map[string]string{"currency": "USDT"}, nil)
	if err != nil {
		return nil, err
	}
	var d struct {
		AvailableBalance json.Number `json:"availableBalance"`
		AccountEquity    json.Number `json:"accountEquity"`
		MarginBalance    json.Number `json:"marginBalance"`
	}
	if err := json.Unmarshal(body, &d); err != nil {
		return nil, errInternal("parse balance", err)
	}
	avail, _ := d.AvailableBalance.Float64()
	total, _ := d.AccountEquity.Float64()
	if total == 0 {
		total, _ = d.MarginBalance.Float64()
	}
	if avail == 0 {
		avail = total
	}
	return &trade.Balance{TotalUSD: total, AvailableUSD: avail}, nil
}

func (a *Adapter) SetLeverage(ctx context.Context, creds trade.Creds, req trade.LeverageRequest) error {
	if !req.MarginMode.IsValid() {
		return errUser("margin_mode invalid")
	}
	if req.Leverage <= 0 {
		return errUser("leverage must be > 0")
	}
	// KuCoin Futures: leverage is set per-order via the `leverage`
	// field in /api/v1/orders. There's no separate set-leverage call
	// on the public futures API. Margin mode is on the order body too.
	// So this method is a no-op — Place/Close pass leverage + marginMode.
	return nil
}

func (a *Adapter) PlaceOrder(ctx context.Context, creds trade.Creds, req trade.OpenRequest) (*trade.Result, error) {
	if err := req.Validate(); err != nil {
		return nil, err
	}
	sym := toKucoinSymbol(req.Symbol)
	info, err := a.instrumentInfo(ctx, sym)
	if err != nil {
		return nil, err
	}
	contracts := coinsToContracts(req.Quantity, info.Multiplier, info.LotSize)
	if contracts <= 0 {
		return nil, errUser("quantity below KuCoin minimum (multiplier=%g)", info.Multiplier)
	}
	side := "buy"
	if req.Side == trade.SideSell {
		side = "sell"
	}
	mgn := "ISOLATED"
	if req.MarginMode == trade.MarginCross {
		mgn = "CROSS"
	}
	orderReq := map[string]any{
		"clientOid":  uuid(),
		"symbol":     sym,
		"side":       side,
		"size":       contracts,
		"leverage":   strconv.Itoa(req.Leverage),
		"marginMode": mgn,
	}
	switch req.OrderType {
	case trade.OrderLimit:
		orderReq["type"] = "limit"
		orderReq["price"] = strconv.FormatFloat(req.LimitPrice, 'f', -1, 64)
		orderReq["timeInForce"] = "GTC"
	case trade.OrderStopMarket, trade.OrderTakeProfitMkt:
		// stop="down" triggers when price drops to stopPrice (SL for long);
		// stop="up" triggers when price rises to stopPrice (TP for long).
		stopDir := "down"
		if req.OrderType == trade.OrderTakeProfitMkt {
			stopDir = "up"
		}
		orderReq["type"] = "market"
		orderReq["stop"] = stopDir
		orderReq["stopPrice"] = strconv.FormatFloat(req.StopPrice, 'f', -1, 64)
		orderReq["stopPriceType"] = "TP"
	default:
		orderReq["type"] = "market"
	}
	endpoint := "/api/v1/orders"
	if req.OrderType.IsConditional() {
		endpoint = "/api/v1/stop-orders"
	}
	body, err := a.signedRequest(ctx, creds, http.MethodPost, endpoint, nil, orderReq)
	if err != nil {
		return nil, err
	}
	var resp struct {
		OrderID  string `json:"orderId"`
		ClientID string `json:"clientOid"`
	}
	_ = json.Unmarshal(body, &resp)
	res := &trade.Result{
		OrderID:       resp.OrderID,
		Symbol:        req.Symbol,
		Side:          req.Side,
		Quantity:      float64(contracts) * info.Multiplier,
		Status:        "NEW",
		ClientOrderID: resp.ClientID,
		CreatedAt:     time.Now().UTC(),
		Raw:           body,
	}
	if resp.OrderID != "" {
		if avg := a.fetchKucoinAvgPrice(ctx, creds, resp.OrderID); avg > 0 {
			res.AvgPrice = avg
		}
	}
	return res, nil
}

func (a *Adapter) fetchKucoinAvgPrice(ctx context.Context, creds trade.Creds, orderID string) float64 {
	timer := time.NewTimer(400 * time.Millisecond)
	defer timer.Stop()
	select {
	case <-timer.C:
	case <-ctx.Done():
		return 0
	}
	data, err := a.signedRequest(ctx, creds, http.MethodGet, "/api/v1/orders/"+orderID, nil, nil)
	if err != nil {
		return 0
	}
	var ord struct {
		DealFunds string `json:"dealFunds"`
		DealSize  string `json:"dealSize"`
		Price     string `json:"price"`
	}
	_ = json.Unmarshal(data, &ord)
	funds, _ := strconv.ParseFloat(ord.DealFunds, 64)
	size, _ := strconv.ParseFloat(ord.DealSize, 64)
	if funds > 0 && size > 0 {
		return funds / size
	}
	px, _ := strconv.ParseFloat(ord.Price, 64)
	return px
}

func (a *Adapter) ClosePosition(ctx context.Context, creds trade.Creds, req trade.CloseRequest) (*trade.Result, error) {
	if req.Symbol == "" {
		return nil, errUser("symbol required")
	}
	sym := toKucoinSymbol(req.Symbol)
	positions, err := a.ListPositions(ctx, creds, req.Symbol)
	if err != nil {
		return nil, err
	}
	if len(positions) == 0 {
		return &trade.Result{Symbol: req.Symbol, Status: "FLAT"}, nil
	}
	p := positions[0]
	reduceSide := "sell"
	if p.Side == trade.SideSell {
		reduceSide = "buy"
	}
	body, err := a.signedRequest(ctx, creds, http.MethodPost, "/api/v1/orders", nil,
		map[string]any{
			"clientOid":  uuid(),
			"symbol":     sym,
			"side":       reduceSide,
			"type":       "market",
			"closeOrder": true,
			"size":       1, // closeOrder ignores size; KuCoin schema requires it
		})
	if err != nil {
		return nil, err
	}
	var resp struct {
		OrderID string `json:"orderId"`
	}
	_ = json.Unmarshal(body, &resp)
	closeSide := trade.SideSell
	if reduceSide == "buy" {
		closeSide = trade.SideBuy
	}
	return &trade.Result{
		OrderID:   resp.OrderID,
		Symbol:    req.Symbol,
		Side:      closeSide,
		Quantity:  p.Quantity,
		Status:    "NEW",
		CreatedAt: time.Now().UTC(),
		Raw:       body,
	}, nil
}

func (a *Adapter) ListPositions(ctx context.Context, creds trade.Creds, symbol string) ([]trade.Position, error) {
	params := map[string]string{}
	if symbol != "" {
		params["symbol"] = toKucoinSymbol(symbol)
	}
	body, err := a.signedRequest(ctx, creds, http.MethodGet, "/api/v1/positions", params, nil)
	if err != nil {
		return nil, err
	}
	var rows []struct {
		Symbol         string      `json:"symbol"`
		CurrentQty     json.Number `json:"currentQty"` // signed: +long, -short
		AvgEntryPrice  json.Number `json:"avgEntryPrice"`
		MarkPrice      json.Number `json:"markPrice"`
		Leverage       json.Number `json:"leverage"`
		UnrealisedPnl  json.Number `json:"unrealisedPnl"`
		CrossMode      bool        `json:"crossMode"`
		MarginMode     string      `json:"marginMode"`
	}
	if err := json.Unmarshal(body, &rows); err != nil {
		return nil, errInternal("parse positions", err)
	}
	out := make([]trade.Position, 0, len(rows))
	for _, p := range rows {
		qty, _ := p.CurrentQty.Float64()
		if qty == 0 {
			continue
		}
		side := trade.SideBuy
		if qty < 0 {
			side = trade.SideSell
		}
		mode := trade.MarginIsolated
		if p.CrossMode || strings.EqualFold(p.MarginMode, "cross") {
			mode = trade.MarginCross
		}
		entry, _ := p.AvgEntryPrice.Float64()
		mark, _ := p.MarkPrice.Float64()
		lev, _ := p.Leverage.Float64()
		upl, _ := p.UnrealisedPnl.Float64()
		// Convert contracts back to coins via instrument multiplier.
		info, _ := a.instrumentInfo(ctx, p.Symbol)
		mult := info.Multiplier
		if mult <= 0 {
			mult = 1
		}
		coins := math.Abs(qty) * mult
		stripped := strings.TrimSuffix(p.Symbol, "USDTM")
		if stripped == "XBT" {
			stripped = "BTC"
		}
		out = append(out, trade.Position{
			Symbol:        stripped,
			Side:          side,
			Quantity:      coins,
			EntryPrice:    entry,
			MarkPrice:     mark,
			Leverage:      int(lev),
			UnrealizedPnL: upl,
			Notional:      coins * mark,
			MarginMode:    mode,
		})
	}
	return out, nil
}

// ── Friendly error mapping ───────────────────────────────────────────────

var friendlyMap = map[string]string{
	"400":    "Bad request — check parameters.",
	"401":    "Invalid API key / signature / passphrase.",
	"403":    "Forbidden — IP whitelist or permissions.",
	"429":    "Rate limit exceeded — try again in a moment.",
	"230003": "Insufficient balance for margin.",
	"230005": "Order qty below contract minimum.",
	"230015": "Position size exceeds account limit.",
	"330011": "Price out of allowed range.",
	"100001": "Invalid request body.",
}

func friendly(code, msg string) string {
	if v, ok := friendlyMap[code]; ok {
		return v
	}
	if msg != "" {
		return msg
	}
	return "KuCoin rejected the request."
}

func errUser(msg string, args ...any) *trade.Error {
	return &trade.Error{Kind: trade.KindUser, Message: fmt.Sprintf(msg, args...)}
}

func errInternal(msg string, cause error) *trade.Error {
	return &trade.Error{Kind: trade.KindInternal, Message: msg, Cause: cause}
}

var _ trade.Adapter = (*Adapter)(nil)
