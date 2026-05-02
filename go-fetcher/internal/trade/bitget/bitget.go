// Bitget V2 USDT-Futures trade adapter.
//
// Port of `backend/services/trade_adapters/bitget.py`.
//
// Signing: base64( HMAC_SHA256(secret, timestamp + method + path-with-query + body) ).
// Headers:
//
//	ACCESS-KEY:        <api_key>
//	ACCESS-SIGN:       <base64 signature>
//	ACCESS-TIMESTAMP:  <ms>
//	ACCESS-PASSPHRASE: <passphrase>
//	Content-Type:      application/json
//	locale:            en-US
//
// Quirks:
//   - Symbol form: "BTCUSDT" (no separator).
//   - Quantity is in COINS (Bitget rounds internally to its sizeMultiplier
//     and volumePlace precision; we round client-side too for clean
//     order display).
//   - Side fields: `side` = buy/sell, `tradeSide` = open/close in
//     "double-position" mode. We use one-way `tradeSide=open` to mirror
//     Python's adapter.
//   - Close uses dedicated /close-positions endpoint (flushes the
//     symbol regardless of mode). Hedge users will see both legs
//     close — accepted.
package bitget

import (
	"context"
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

const baseURL = "https://api.bitget.com"

type Adapter struct {
	httpClient *http.Client

	infoMu sync.RWMutex
	info   map[string]instrumentInfo
}

type instrumentInfo struct {
	SizeMultiplier float64
	VolumePlace    int
	MinTradeNum    float64
	At             time.Time
}

const infoTTL = 10 * time.Minute

func New() *Adapter {
	return &Adapter{
		httpClient: &http.Client{
			Timeout: 15 * time.Second,
			Transport: &http.Transport{
				MaxIdleConnsPerHost: 8,
				IdleConnTimeout:     60 * time.Second,
			},
		},
		info: make(map[string]instrumentInfo, 256),
	}
}

func init() { trade.Register("bitget", New()) }

func (a *Adapter) Name() string { return "bitget" }

func toBitgetSymbol(sym string) string { return strings.ToUpper(sym) + "USDT" }

// ── Signing ──────────────────────────────────────────────────────────────

func (a *Adapter) signedRequest(
	ctx context.Context, creds trade.Creds, method, path string,
	params map[string]string, body any,
) (json.RawMessage, error) {
	if creds.Passphrase == "" {
		return nil, errUser("Bitget requires passphrase credential")
	}
	ts := strconv.FormatInt(time.Now().UnixMilli(), 10)

	signPath := path
	bodyStr := ""
	if method == http.MethodGet && len(params) > 0 {
		signPath = path + "?" + trade.SortedFormQuery(params)
	} else if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return nil, errInternal("marshal body", err)
		}
		bodyStr = string(b)
	}
	sig := trade.HMACBase64SHA256(creds.APISecret, ts+method+signPath+bodyStr)

	url := baseURL + signPath
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
	req.Header.Set("ACCESS-KEY", creds.APIKey)
	req.Header.Set("ACCESS-SIGN", sig)
	req.Header.Set("ACCESS-TIMESTAMP", ts)
	req.Header.Set("ACCESS-PASSPHRASE", creds.Passphrase)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("locale", "en-US")

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
	if env.Code != "00000" && env.Code != "" {
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
	url := baseURL + "/api/v2/mix/market/contracts?productType=USDT-FUTURES&symbol=" + sym
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
		Data []struct {
			Symbol         string `json:"symbol"`
			SizeMultiplier string `json:"sizeMultiplier"`
			VolumePlace    string `json:"volumePlace"`
			MinTradeNum    string `json:"minTradeNum"`
		} `json:"data"`
	}
	if err := json.Unmarshal(raw, &env); err != nil || len(env.Data) == 0 {
		return instrumentInfo{}, &trade.Error{
			Kind:    trade.KindUser,
			Message: fmt.Sprintf("symbol %s not listed on Bitget", sym),
		}
	}
	d := env.Data[0]
	szm, _ := strconv.ParseFloat(d.SizeMultiplier, 64)
	vp, _ := strconv.Atoi(d.VolumePlace)
	mtn, _ := strconv.ParseFloat(d.MinTradeNum, 64)
	if szm <= 0 {
		szm = 1
	}
	if vp < 0 {
		vp = 0
	}
	out := instrumentInfo{
		SizeMultiplier: szm,
		VolumePlace:    vp,
		MinTradeNum:    mtn,
		At:             time.Now(),
	}
	a.infoMu.Lock()
	a.info[sym] = out
	a.infoMu.Unlock()
	return out, nil
}

// ── Quantity helpers ─────────────────────────────────────────────────────

func roundToMultiplier(qty, mult float64, place int) float64 {
	if mult > 0 {
		qty = math.Floor(qty/mult) * mult
	}
	if place < 0 {
		place = 0
	}
	factor := math.Pow10(place)
	return math.Floor(qty*factor) / factor
}

func qtyString(q float64, place int) string {
	if place < 0 {
		place = 0
	}
	s := strconv.FormatFloat(q, 'f', place, 64)
	if strings.Contains(s, ".") {
		s = strings.TrimRight(s, "0")
		s = strings.TrimRight(s, ".")
		if s == "" {
			s = "0"
		}
	}
	return s
}

// ── Adapter methods ──────────────────────────────────────────────────────

func (a *Adapter) GetBalance(ctx context.Context, creds trade.Creds) (*trade.Balance, error) {
	body, err := a.signedRequest(ctx, creds, http.MethodGet,
		"/api/v2/mix/account/accounts",
		map[string]string{"productType": "USDT-FUTURES"}, nil)
	if err != nil {
		return nil, err
	}
	var rows []struct {
		MarginCoin           string `json:"marginCoin"`
		Available            string `json:"available"`
		CrossedMaxAvailable  string `json:"crossedMaxAvailable"`
		Equity               string `json:"equity"`
	}
	if err := json.Unmarshal(body, &rows); err != nil {
		return nil, errInternal("parse balance", err)
	}
	for _, r := range rows {
		if r.MarginCoin != "USDT" {
			continue
		}
		avail, _ := strconv.ParseFloat(r.Available, 64)
		if avail == 0 {
			avail, _ = strconv.ParseFloat(r.CrossedMaxAvailable, 64)
		}
		total, _ := strconv.ParseFloat(r.Equity, 64)
		if total == 0 {
			total = avail
		}
		return &trade.Balance{TotalUSD: total, AvailableUSD: avail}, nil
	}
	return &trade.Balance{}, nil
}

func (a *Adapter) SetLeverage(ctx context.Context, creds trade.Creds, req trade.LeverageRequest) error {
	if !req.MarginMode.IsValid() {
		return errUser("margin_mode invalid")
	}
	if req.Leverage <= 0 {
		return errUser("leverage must be > 0")
	}
	sym := toBitgetSymbol(req.Symbol)
	bgMode := "isolated"
	if req.MarginMode == trade.MarginCross {
		bgMode = "crossed"
	}
	// Bitget needs margin-mode set first (separate endpoint), then leverage.
	_, err := a.signedRequest(ctx, creds, http.MethodPost,
		"/api/v2/mix/account/set-margin-mode", nil, map[string]any{
			"symbol":      sym,
			"productType": "USDT-FUTURES",
			"marginCoin":  "USDT",
			"marginMode":  bgMode,
		})
	if err != nil {
		te, ok := err.(*trade.Error)
		// Already-set codes — accept.
		if !ok || (te.Code != "45117" && te.Code != "40925" &&
			!strings.Contains(strings.ToLower(te.Message), "no need")) {
			// Don't abort the leverage call on margin-mode failures —
			// the position might still open with current mode.
		}
	}
	_, err = a.signedRequest(ctx, creds, http.MethodPost,
		"/api/v2/mix/account/set-leverage", nil, map[string]any{
			"symbol":      sym,
			"productType": "USDT-FUTURES",
			"marginCoin":  "USDT",
			"leverage":    strconv.Itoa(req.Leverage),
		})
	if err != nil {
		te, ok := err.(*trade.Error)
		if ok && (strings.Contains(strings.ToLower(te.Message), "no need") ||
			te.Code == "45117") {
			return nil
		}
	}
	return err
}

func (a *Adapter) PlaceOrder(ctx context.Context, creds trade.Creds, req trade.OpenRequest) (*trade.Result, error) {
	if err := req.Validate(); err != nil {
		return nil, err
	}
	sym := toBitgetSymbol(req.Symbol)
	info, err := a.instrumentInfo(ctx, sym)
	if err != nil {
		return nil, err
	}
	qty := roundToMultiplier(req.Quantity, info.SizeMultiplier, info.VolumePlace)
	if qty <= 0 || (info.MinTradeNum > 0 && qty < info.MinTradeNum) {
		return nil, errUser("quantity below Bitget minimum (%g %s)", info.MinTradeNum, req.Symbol)
	}
	bgMode := "isolated"
	if req.MarginMode == trade.MarginCross {
		bgMode = "crossed"
	}
	side := "buy"
	if req.Side == trade.SideSell {
		side = "sell"
	}
	body, err := a.signedRequest(ctx, creds, http.MethodPost,
		"/api/v2/mix/order/place-order", nil, map[string]any{
			"symbol":      sym,
			"productType": "USDT-FUTURES",
			"marginMode":  bgMode,
			"marginCoin":  "USDT",
			"side":        side,
			"tradeSide":   "open",
			"orderType":   "market",
			"size":        qtyString(qty, info.VolumePlace),
		})
	if err != nil {
		return nil, err
	}
	var resp struct {
		OrderID     string `json:"orderId"`
		ClientOrdID string `json:"clientOid"`
	}
	_ = json.Unmarshal(body, &resp)
	return &trade.Result{
		OrderID:       resp.OrderID,
		Symbol:        req.Symbol,
		Side:          req.Side,
		Quantity:      qty,
		Status:        "NEW",
		ClientOrderID: resp.ClientOrdID,
		CreatedAt:     time.Now().UTC(),
		Raw:           body,
	}, nil
}

func (a *Adapter) ClosePosition(ctx context.Context, creds trade.Creds, req trade.CloseRequest) (*trade.Result, error) {
	if req.Symbol == "" {
		return nil, errUser("symbol required")
	}
	sym := toBitgetSymbol(req.Symbol)
	positions, err := a.ListPositions(ctx, creds, req.Symbol)
	if err != nil {
		return nil, err
	}
	if len(positions) == 0 {
		return &trade.Result{Symbol: req.Symbol, Status: "FLAT"}, nil
	}
	p := positions[0]
	if req.Side != "" {
		for _, q := range positions {
			if q.Side == req.Side {
				p = q
				break
			}
		}
	}
	body, err := a.signedRequest(ctx, creds, http.MethodPost,
		"/api/v2/mix/order/close-positions", nil, map[string]any{
			"symbol":      sym,
			"productType": "USDT-FUTURES",
		})
	if err != nil {
		return nil, err
	}
	var resp struct {
		SuccessList []struct {
			OrderID string `json:"orderId"`
		} `json:"successList"`
	}
	_ = json.Unmarshal(body, &resp)
	orderID := ""
	if len(resp.SuccessList) > 0 {
		orderID = resp.SuccessList[0].OrderID
	}
	closeSide := trade.SideSell
	if p.Side == trade.SideSell {
		closeSide = trade.SideBuy
	}
	return &trade.Result{
		OrderID:   orderID,
		Symbol:    req.Symbol,
		Side:      closeSide,
		Quantity:  p.Quantity,
		Status:    "NEW",
		CreatedAt: time.Now().UTC(),
		Raw:       body,
	}, nil
}

func (a *Adapter) ListPositions(ctx context.Context, creds trade.Creds, symbol string) ([]trade.Position, error) {
	params := map[string]string{"productType": "USDT-FUTURES"}
	if symbol != "" {
		params["symbol"] = toBitgetSymbol(symbol)
	}
	body, err := a.signedRequest(ctx, creds, http.MethodGet,
		"/api/v2/mix/position/all-position", params, nil)
	if err != nil {
		return nil, err
	}
	var rows []struct {
		Symbol         string `json:"symbol"`
		HoldSide       string `json:"holdSide"` // long / short
		Total          string `json:"total"`
		Available      string `json:"available"`
		OpenPriceAvg   string `json:"openPriceAvg"`
		MarkPrice      string `json:"markPrice"`
		Leverage       string `json:"leverage"`
		MarginMode     string `json:"marginMode"`
		UnrealizedPL   string `json:"unrealizedPL"`
	}
	if err := json.Unmarshal(body, &rows); err != nil {
		return nil, errInternal("parse positions", err)
	}
	out := make([]trade.Position, 0, len(rows))
	for _, p := range rows {
		qty, _ := strconv.ParseFloat(p.Total, 64)
		if qty == 0 {
			qty, _ = strconv.ParseFloat(p.Available, 64)
		}
		if qty == 0 {
			continue
		}
		side := trade.SideBuy
		if strings.EqualFold(p.HoldSide, "short") {
			side = trade.SideSell
		}
		mode := trade.MarginIsolated
		if strings.EqualFold(p.MarginMode, "crossed") || strings.EqualFold(p.MarginMode, "cross") {
			mode = trade.MarginCross
		}
		entry, _ := strconv.ParseFloat(p.OpenPriceAvg, 64)
		mark, _ := strconv.ParseFloat(p.MarkPrice, 64)
		lev, _ := strconv.ParseFloat(p.Leverage, 64)
		upl, _ := strconv.ParseFloat(p.UnrealizedPL, 64)
		stripped := strings.TrimSuffix(p.Symbol, "USDT")
		out = append(out, trade.Position{
			Symbol:        stripped,
			Side:          side,
			Quantity:      qty,
			EntryPrice:    entry,
			MarkPrice:     mark,
			Leverage:      int(lev),
			UnrealizedPnL: upl,
			Notional:      qty * mark,
			MarginMode:    mode,
		})
	}
	return out, nil
}

// ── Friendly error mapping ───────────────────────────────────────────────

var friendlyMap = map[string]string{
	"40001": "Invalid API key.",
	"40002": "Invalid signature.",
	"40009": "API key permissions insufficient.",
	"40010": "API key passphrase incorrect.",
	"40037": "Symbol not found on Bitget.",
	"40060": "API key bound to different IP.",
	"40913": "Insufficient margin for this order.",
	"45117": "Margin mode / leverage not changed (already set).",
	"40925": "Leverage already at requested value.",
	"40792": "Order qty below minimum.",
	"45034": "Order rejected — qty below contract minimum.",
}

func friendly(code, msg string) string {
	if v, ok := friendlyMap[code]; ok {
		return v
	}
	if msg != "" {
		return msg
	}
	return "Bitget rejected the request."
}

func errUser(msg string, args ...any) *trade.Error {
	return &trade.Error{Kind: trade.KindUser, Message: fmt.Sprintf(msg, args...)}
}

func errInternal(msg string, cause error) *trade.Error {
	return &trade.Error{Kind: trade.KindInternal, Message: msg, Cause: cause}
}

var _ trade.Adapter = (*Adapter)(nil)
