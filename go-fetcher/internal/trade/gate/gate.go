// Gate.io Futures (USDT-M) trade adapter.
//
// Port of `backend/services/trade_adapters/gate.py`.
//
// Signing: HMAC-SHA512 hex of
//
//	method "\n" path "\n" sortedQuery "\n" sha512(body) "\n" timestamp_seconds
//
// Headers:
//
//	KEY:        <api_key>
//	SIGN:       <hex digest>
//	Timestamp:  <unix seconds>
//	Content-Type: application/json
//
// Quirks:
//   - Symbol form: "BTC_USDT".
//   - Quantity is in CONTRACTS, not coins. Each contract has a
//     `quanto_multiplier` (e.g. BTC_USDT = 0.0001 BTC). qty_coins ÷
//     quanto = contracts. Position output converts back to coins.
//   - Order side encoded in the sign of `size` (positive = long,
//     negative = short). No explicit "side" field.
//   - Leverage endpoint takes leverage as QUERY param, not body.
//   - Cross mode = leverage 0; isolated = leverage > 0.
//   - Close path tries single-mode first, falls back to dual-mode
//     `auto_size` on POSITION_DUAL_MODE error.
package gate

import (
	"context"
	"crypto/sha512"
	"encoding/hex"
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

const baseURL = "https://api.gateio.ws"

type Adapter struct {
	httpClient *http.Client

	contractsMu sync.RWMutex
	contracts   map[string]contract
	contractsAt time.Time
}

type contract struct {
	QuantoMultiplier float64
	OrderSizeMin     int64
	OrderSizeMax     int64
	EnableDecimal    bool
}

const contractsTTL = 10 * time.Minute

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
		contracts: make(map[string]contract, 256),
	}
}

func init() {
	a := New()
	trade.Register("gate", a)
	go func() {
		time.Sleep(2 * time.Second)
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_, _ = a.loadContracts(ctx)
	}()
}

func (a *Adapter) Name() string { return "gate" }

// ── Symbol mapping ───────────────────────────────────────────────────────

func toGateSymbol(sym string) string { return strings.ToUpper(sym) + "_USDT" }

// ── Signing ──────────────────────────────────────────────────────────────

func sha512Hex(s string) string {
	h := sha512.Sum512([]byte(s))
	return hex.EncodeToString(h[:])
}

func gateSign(secret, method, path, query, body, ts string) string {
	bodyHash := sha512Hex(body)
	pre := method + "\n" + path + "\n" + query + "\n" + bodyHash + "\n" + ts
	return hex.EncodeToString(trade.HMACWith(sha512.New, secret, pre))
}

func (a *Adapter) signedRequest(
	ctx context.Context, creds trade.Creds, method, path string,
	query map[string]string, body any,
) (json.RawMessage, error) {
	ts := strconv.FormatInt(time.Now().Unix(), 10)
	queryStr := trade.SortedFormQuery(query)
	bodyStr := ""
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return nil, errInternal("marshal body", err)
		}
		bodyStr = string(b)
	}
	sig := gateSign(creds.APISecret, method, path, queryStr, bodyStr, ts)

	url := baseURL + path
	if queryStr != "" {
		url += "?" + queryStr
	}
	req, err := http.NewRequestWithContext(ctx, method, url, strings.NewReader(bodyStr))
	if err != nil {
		return nil, err
	}
	req.Header.Set("KEY", creds.APIKey)
	req.Header.Set("SIGN", sig)
	req.Header.Set("Timestamp", ts)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")

	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, &trade.Error{Kind: trade.KindTransient, Message: err.Error(), Cause: err}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)

	if resp.StatusCode == 204 || len(raw) == 0 {
		return json.RawMessage("null"), nil
	}
	if resp.StatusCode >= 400 {
		return nil, parseError(resp.StatusCode, raw)
	}
	return raw, nil
}

func parseError(status int, body []byte) *trade.Error {
	var env struct {
		Label   string `json:"label"`
		Message string `json:"message"`
		Detail  string `json:"detail"`
	}
	_ = json.Unmarshal(body, &env)
	msg := env.Message
	if msg == "" {
		msg = env.Detail
	}
	if msg == "" {
		msg = strings.TrimSpace(string(body))
	}
	if status == 429 {
		return &trade.Error{Kind: trade.KindRateLimit, Code: env.Label, Message: friendly(env.Label, msg)}
	}
	return &trade.Error{Kind: trade.KindExchange, Code: env.Label, Message: friendly(env.Label, msg)}
}

// ── Contract cache ───────────────────────────────────────────────────────

func (a *Adapter) loadContracts(ctx context.Context) (map[string]contract, error) {
	a.contractsMu.RLock()
	cached := a.contracts
	at := a.contractsAt
	a.contractsMu.RUnlock()
	if cached != nil && time.Since(at) < contractsTTL {
		return cached, nil
	}
	url := baseURL + "/api/v4/futures/usdt/contracts"
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	resp, err := a.httpClient.Do(req)
	if err != nil {
		if cached != nil {
			return cached, nil
		}
		return nil, &trade.Error{Kind: trade.KindTransient, Message: err.Error()}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return nil, parseError(resp.StatusCode, raw)
	}
	var rows []struct {
		Name             string `json:"name"`
		QuantoMultiplier string `json:"quanto_multiplier"`
		OrderSizeMin     int64  `json:"order_size_min"`
		OrderSizeMax     int64  `json:"order_size_max"`
		EnableDecimal    bool   `json:"enable_decimal"`
	}
	if err := json.Unmarshal(raw, &rows); err != nil {
		return nil, errInternal("parse contracts", err)
	}
	out := make(map[string]contract, len(rows))
	for _, r := range rows {
		if !strings.HasSuffix(r.Name, "_USDT") {
			continue
		}
		qm, _ := strconv.ParseFloat(r.QuantoMultiplier, 64)
		out[r.Name] = contract{
			QuantoMultiplier: qm,
			OrderSizeMin:     r.OrderSizeMin,
			OrderSizeMax:     r.OrderSizeMax,
			EnableDecimal:    r.EnableDecimal,
		}
	}
	a.contractsMu.Lock()
	a.contracts = out
	a.contractsAt = time.Now()
	a.contractsMu.Unlock()
	return out, nil
}

// ── Quantity conversion ──────────────────────────────────────────────────

func coinsToContracts(qty, quanto float64) int64 {
	if quanto <= 0 {
		return int64(math.Floor(qty))
	}
	return int64(math.Floor(qty / quanto))
}

// ── Adapter methods ──────────────────────────────────────────────────────

func (a *Adapter) GetBalance(ctx context.Context, creds trade.Creds) (*trade.Balance, error) {
	body, err := a.signedRequest(ctx, creds, http.MethodGet,
		"/api/v4/futures/usdt/accounts", nil, nil)
	if err != nil {
		return nil, err
	}
	var resp struct {
		Available string `json:"available"`
		Total     string `json:"total"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		return nil, errInternal("parse balance", err)
	}
	avail, _ := strconv.ParseFloat(resp.Available, 64)
	total, _ := strconv.ParseFloat(resp.Total, 64)
	return &trade.Balance{TotalUSD: total, AvailableUSD: avail}, nil
}

func (a *Adapter) SetLeverage(ctx context.Context, creds trade.Creds, req trade.LeverageRequest) error {
	if !req.MarginMode.IsValid() {
		return errUser("margin_mode invalid")
	}
	if req.Leverage <= 0 {
		return errUser("leverage must be > 0")
	}
	contract := toGateSymbol(req.Symbol)
	// 0 = cross; >0 = isolated leverage value
	levVal := strconv.Itoa(req.Leverage)
	if req.MarginMode == trade.MarginCross {
		levVal = "0"
	}
	path := "/api/v4/futures/usdt/positions/" + contract + "/leverage"
	_, err := a.signedRequest(ctx, creds, http.MethodPost, path,
		map[string]string{"leverage": levVal}, nil)
	if err != nil {
		te, ok := err.(*trade.Error)
		// "not changed" / "same value" — non-fatal, leverage already what we want.
		if ok && (strings.Contains(strings.ToLower(te.Message), "not changed") ||
			strings.Contains(strings.ToLower(te.Message), "same")) {
			return nil
		}
		return err
	}
	// In isolated mode, also set cross_leverage_limit=0 so a future
	// switch into cross uses the right initial value.
	if req.MarginMode == trade.MarginIsolated {
		_, _ = a.signedRequest(ctx, creds, http.MethodPost, path,
			map[string]string{
				"leverage":             strconv.Itoa(req.Leverage),
				"cross_leverage_limit": "0",
			}, nil)
	}
	return nil
}

func (a *Adapter) PlaceOrder(ctx context.Context, creds trade.Creds, req trade.OpenRequest) (*trade.Result, error) {
	if err := req.Validate(); err != nil {
		return nil, err
	}
	contract := toGateSymbol(req.Symbol)
	contracts, err := a.loadContracts(ctx)
	if err != nil {
		return nil, err
	}
	info, ok := contracts[contract]
	if !ok {
		return nil, errUser("symbol %s is not listed on Gate.io futures", contract)
	}
	// Gate supports fractional contracts when EnableDecimal is true
	// (most USDT-perp pairs on modern Gate). Otherwise force integer.
	var sizeJSON any
	if info.EnableDecimal {
		raw := req.Quantity
		if info.QuantoMultiplier > 0 {
			raw = req.Quantity / info.QuantoMultiplier
		}
		// Gate enable_decimal accepts only 1-decimal-place granularity in
		// practice (0.1 OK, 0.15 → "invalid size"). Round DOWN to 0.1.
		raw = math.Floor(raw*10) / 10
		if raw <= 0 {
			return nil, errUser("quantity too small for %s (rounds to 0 contracts)", contract)
		}
		if req.Side == trade.SideSell {
			raw = -raw
		}
		sizeJSON = strconv.FormatFloat(raw, 'f', -1, 64)
	} else {
		num := coinsToContracts(req.Quantity, info.QuantoMultiplier)
		if num <= 0 || num < info.OrderSizeMin {
			return nil, errUser("quantity too small for %s (min %d contracts)", contract, info.OrderSizeMin)
		}
		if req.Side == trade.SideSell {
			num = -num
		}
		sizeJSON = num
	}
	size := sizeJSON
	var orderBody map[string]any
	switch req.OrderType {
	case trade.OrderLimit:
		orderBody = map[string]any{
			"contract": contract,
			"size":     size,
			"price":    strconv.FormatFloat(req.LimitPrice, 'f', -1, 64),
			"tif":      "gtc",
		}
	case trade.OrderStopMarket, trade.OrderTakeProfitMkt:
		// Gate conditional orders use a separate price_orders endpoint.
		// rule: 1=price>=trigger (TP for long), 2=price<=trigger (SL for long)
		rule := 2 // stop_market default: trigger when price drops
		if req.OrderType == trade.OrderTakeProfitMkt {
			rule = 1
		}
		condBody := map[string]any{
			"trigger": map[string]any{
				"strategy_type": 0, // by_price
				"price_type":    0, // last_price
				"price":         strconv.FormatFloat(req.StopPrice, 'f', -1, 64),
				"rule":          rule,
				"expiration":    86400,
			},
			"order": map[string]any{
				"contract": contract,
				"size":     size,
				"price":    "0",
				"tif":      "ioc",
			},
		}
		condBody2, err := a.signedRequest(ctx, creds, http.MethodPost,
			"/api/v4/futures/usdt/price_orders", nil, condBody)
		if err != nil {
			return nil, err
		}
		var condResp struct {
			ID json.Number `json:"id"`
		}
		_ = json.Unmarshal(condBody2, &condResp)
		return &trade.Result{
			OrderID:   string(condResp.ID),
			Symbol:    req.Symbol,
			Side:      req.Side,
			Quantity:  req.Quantity,
			Status:    "PENDING",
			CreatedAt: time.Now().UTC(),
			Raw:       condBody2,
		}, nil
	default:
		orderBody = map[string]any{
			"contract": contract,
			"size":     size,
			"price":    "0",
			"tif":      "ioc",
		}
	}
	body, err := a.signedRequest(ctx, creds, http.MethodPost,
		"/api/v4/futures/usdt/orders", nil, orderBody)
	if err != nil {
		return nil, err
	}
	var resp struct {
		ID        json.Number `json:"id"`
		FillPrice string      `json:"fill_price"`
		Status    string      `json:"status"`
	}
	_ = json.Unmarshal(body, &resp)
	fill, _ := strconv.ParseFloat(resp.FillPrice, 64)
	return &trade.Result{
		OrderID:   string(resp.ID),
		Symbol:    req.Symbol,
		Side:      req.Side,
		Quantity:  req.Quantity,
		AvgPrice:  fill,
		Status:    resp.Status,
		CreatedAt: time.Now().UTC(),
		Raw:       body,
	}, nil
}

func (a *Adapter) ClosePosition(ctx context.Context, creds trade.Creds, req trade.CloseRequest) (*trade.Result, error) {
	if req.Symbol == "" {
		return nil, errUser("symbol required")
	}
	contract := toGateSymbol(req.Symbol)
	positions, err := a.ListPositions(ctx, creds, req.Symbol)
	if err != nil {
		return nil, err
	}
	if len(positions) == 0 {
		return &trade.Result{Symbol: req.Symbol, Status: "FLAT"}, nil
	}
	// Pick the matching leg in dual-mode. In single-mode there's only one.
	var p trade.Position
	if req.Side != "" {
		for _, q := range positions {
			if q.Side == req.Side {
				p = q
				break
			}
		}
		if p.Symbol == "" {
			p = positions[0]
		}
	} else {
		p = positions[0]
	}
	// Single-mode close: size=0 + close=true auto-flattens.
	body, err := a.signedRequest(ctx, creds, http.MethodPost,
		"/api/v4/futures/usdt/orders", nil, map[string]any{
			"contract": contract,
			"size":     0,
			"price":    "0",
			"tif":      "ioc",
			"close":    true,
		})
	if err != nil {
		te, ok := err.(*trade.Error)
		if !ok || te.Code != "POSITION_DUAL_MODE" {
			return nil, err
		}
		// Dual-mode: explicit auto_size telling Gate which leg to close.
		autoSize := "close_long"
		if p.Side == trade.SideSell {
			autoSize = "close_short"
		}
		body, err = a.signedRequest(ctx, creds, http.MethodPost,
			"/api/v4/futures/usdt/orders", nil, map[string]any{
				"contract":    contract,
				"size":        0,
				"price":       "0",
				"tif":         "ioc",
				"auto_size":   autoSize,
				"reduce_only": true,
			})
		if err != nil {
			return nil, err
		}
	}
	var resp struct {
		ID        json.Number `json:"id"`
		FillPrice string      `json:"fill_price"`
	}
	_ = json.Unmarshal(body, &resp)
	closeSide := trade.SideSell
	if p.Side == trade.SideSell {
		closeSide = trade.SideBuy
	}
	fill, _ := strconv.ParseFloat(resp.FillPrice, 64)
	return &trade.Result{
		OrderID:   string(resp.ID),
		Symbol:    req.Symbol,
		Side:      closeSide,
		Quantity:  p.Quantity,
		AvgPrice:  fill,
		Status:    "NEW",
		CreatedAt: time.Now().UTC(),
		Raw:       body,
	}, nil
}

func (a *Adapter) ListPositions(ctx context.Context, creds trade.Creds, symbol string) ([]trade.Position, error) {
	body, err := a.signedRequest(ctx, creds, http.MethodGet,
		"/api/v4/futures/usdt/positions", nil, nil)
	if err != nil {
		return nil, err
	}
	var rows []struct {
		Contract       string `json:"contract"`
		Size           int64  `json:"size"`
		EntryPrice     string `json:"entry_price"`
		MarkPrice      string `json:"mark_price"`
		Leverage       string `json:"leverage"`
		Mode           string `json:"mode"` // "single" / "dual_long" / "dual_short"
		UnrealisedPnl  string `json:"unrealised_pnl"`
		Margin         string `json:"margin"`
		CrossLeverage  string `json:"cross_leverage_limit"`
	}
	if err := json.Unmarshal(body, &rows); err != nil {
		return nil, errInternal("parse positions", err)
	}
	contracts, _ := a.loadContracts(ctx)
	wantSym := ""
	if symbol != "" {
		wantSym = toGateSymbol(symbol)
	}
	out := make([]trade.Position, 0, len(rows))
	for _, r := range rows {
		if r.Size == 0 {
			continue
		}
		if wantSym != "" && r.Contract != wantSym {
			continue
		}
		quanto := 1.0
		if c, ok := contracts[r.Contract]; ok && c.QuantoMultiplier > 0 {
			quanto = c.QuantoMultiplier
		}
		side := trade.SideBuy
		if r.Size < 0 {
			side = trade.SideSell
		}
		mode := trade.MarginIsolated
		// Gate uses cross_leverage_limit > 0 to indicate cross mode.
		if cl, _ := strconv.ParseFloat(r.CrossLeverage, 64); cl > 0 {
			mode = trade.MarginCross
		}
		stripped := strings.TrimSuffix(r.Contract, "_USDT")
		entry, _ := strconv.ParseFloat(r.EntryPrice, 64)
		mark, _ := strconv.ParseFloat(r.MarkPrice, 64)
		lev, _ := strconv.ParseFloat(r.Leverage, 64)
		upl, _ := strconv.ParseFloat(r.UnrealisedPnl, 64)
		absSize := math.Abs(float64(r.Size))
		out = append(out, trade.Position{
			Symbol:        stripped,
			Side:          side,
			Quantity:      absSize * quanto,
			EntryPrice:    entry,
			MarkPrice:     mark,
			Leverage:      int(lev),
			UnrealizedPnL: upl,
			Notional:      absSize * quanto * mark,
			MarginMode:    mode,
		})
	}
	return out, nil
}

// ── Friendly error mapping ───────────────────────────────────────────────

var friendlyMap = map[string]string{
	"INVALID_KEY":           "Invalid API key.",
	"INVALID_SIGNATURE":     "Signature mismatch — API secret is wrong.",
	"INSUFFICIENT_BALANCE":  "Insufficient margin for this order.",
	"INVALID_ARGUMENT":      "Invalid order parameter — check size / price / contract.",
	"POSITION_DUAL_MODE":    "Account is in dual position mode (handled internally).",
	"POSITION_NOT_OPEN":     "No open position to close.",
	"NO_TRADE_PERMISSION":   "API key has no trading permission.",
	"NOT_FOUND":             "Contract not found on Gate.io futures.",
	"FORBIDDEN":             "Action forbidden — check IP whitelist + key permissions.",
	"TOO_MANY_REQUESTS":     "Rate limit exceeded — try again in a moment.",
	"ORDER_SIZE_TOO_SMALL":  "Order size below contract minimum.",
}

func friendly(code, msg string) string {
	if v, ok := friendlyMap[code]; ok {
		return v
	}
	if msg != "" {
		return msg
	}
	return "Gate.io rejected the request."
}

// ── Local errors ─────────────────────────────────────────────────────────

func errUser(msg string, args ...any) *trade.Error {
	return &trade.Error{Kind: trade.KindUser, Message: fmt.Sprintf(msg, args...)}
}

func errInternal(msg string, cause error) *trade.Error {
	return &trade.Error{Kind: trade.KindInternal, Message: msg, Cause: cause}
}

var _ trade.Adapter = (*Adapter)(nil)
