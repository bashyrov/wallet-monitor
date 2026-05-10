// MEXC USDT-M Futures (contract.mexc.com) trade adapter.
//
// Port of `backend/services/trade_adapters/mexc.py`.
//
// Signing: HMAC-SHA256 hex of (apiKey + timestamp_ms + paramStr).
// Headers: ApiKey, Request-Time, Signature, Content-Type=application/json.
//
// Quirks:
//   - Symbol form: "BTC_USDT" (underscore).
//   - Quantity is in CONTRACTS (`contractSize` coins each, lots of `volUnit`).
//     qty_coins / contractSize → contracts, then floor to volUnit step.
//     Position output converts back to coins.
//   - Side encoded as integer:
//       1 = open_long   2 = close_short (close a long)
//       3 = open_short  4 = close_long  (close a short)
//   - Margin mode encoded as `openType`: 1 = isolated, 2 = cross.
//   - Type 5 = market.
//   - MEXC returns text/html (Akamai 403) for blocked IPs — we surface
//     a clean error instead of a JSONDecodeError.
package mexc

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

const baseURL = "https://contract.mexc.com"

type Adapter struct {
	httpClient *http.Client

	infoMu sync.RWMutex
	info   map[string]instrumentInfo
}

type instrumentInfo struct {
	MinVol       int64
	VolUnit      int64
	ContractSize float64
	MaxLeverage  int
	At           time.Time
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

func init() { trade.Register("mexc", New()) }

func (a *Adapter) Name() string { return "mexc" }

func toMexcSymbol(sym string) string { return strings.ToUpper(sym) + "_USDT" }

// ── Signing ──────────────────────────────────────────────────────────────

func (a *Adapter) signedRequest(
	ctx context.Context, creds trade.Creds, method, path string,
	params map[string]string, body any,
) (json.RawMessage, error) {
	ts := strconv.FormatInt(time.Now().UnixMilli(), 10)

	// MEXC sign-payload: GET → sortedQuery; POST → JSON body.
	var paramStr string
	var bodyStr string
	if method == http.MethodGet && len(params) > 0 {
		paramStr = trade.SortedFormQuery(params)
	} else if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return nil, errInternal("marshal body", err)
		}
		bodyStr = string(b)
		paramStr = bodyStr
	}
	sig := trade.HMACHexSHA256(creds.APISecret, creds.APIKey+ts+paramStr)

	url := baseURL + path
	if method == http.MethodGet && paramStr != "" {
		url += "?" + paramStr
	}

	req, err := http.NewRequestWithContext(ctx, method, url, strings.NewReader(bodyStr))
	if err != nil {
		return nil, err
	}
	req.Header.Set("ApiKey", creds.APIKey)
	req.Header.Set("Request-Time", ts)
	req.Header.Set("Signature", sig)
	req.Header.Set("Content-Type", "application/json")

	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, &trade.Error{Kind: trade.KindTransient, Message: err.Error(), Cause: err}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)

	// Edge-block detection: MEXC's CDN replies with text/html "Access
	// Denied" for blocked IPs. Fall through to JSON parse and the
	// user gets a meaningless decode error otherwise.
	ct := strings.ToLower(resp.Header.Get("Content-Type"))
	if !strings.Contains(ct, "application/json") {
		if resp.StatusCode == 403 || strings.Contains(string(raw), "Access Denied") {
			return nil, &trade.Error{
				Kind:    trade.KindExchange,
				Message: "MEXC blocked at edge — REST trading not available from this IP. Use a region MEXC accepts (Asia AWS).",
			}
		}
		return nil, &trade.Error{
			Kind:    trade.KindExchange,
			Message: fmt.Sprintf("MEXC HTTP %d: non-JSON response (likely edge-blocked)", resp.StatusCode),
		}
	}

	var env struct {
		Code int             `json:"code"`
		Msg  string          `json:"msg"`
		Data json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(raw, &env); err != nil {
		return nil, errInternal("parse envelope", err)
	}
	if env.Code != 0 {
		codeStr := strconv.Itoa(env.Code)
		msg := friendly(codeStr, env.Msg)
		// 510 series = rate-limit on MEXC.
		if env.Code == 510 || resp.StatusCode == 429 {
			return nil, &trade.Error{Kind: trade.KindRateLimit, Code: codeStr, Message: msg}
		}
		return nil, &trade.Error{Kind: trade.KindExchange, Code: codeStr, Message: msg}
	}
	return env.Data, nil
}

// ── Instrument cache ─────────────────────────────────────────────────────

func (a *Adapter) instrumentInfo(ctx context.Context, sym string) (instrumentInfo, error) {
	a.infoMu.RLock()
	hit, ok := a.info[sym]
	a.infoMu.RUnlock()
	if ok && time.Since(hit.At) < infoTTL {
		return hit, nil
	}
	url := baseURL + "/api/v1/contract/detail?symbol=" + sym
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
		Code int    `json:"code"`
		Data struct {
			MinVol       json.Number `json:"minVol"`
			MaxVol       json.Number `json:"maxVol"`
			ContractSize json.Number `json:"contractSize"`
			VolUnit      json.Number `json:"volUnit"`
			MaxLeverage  json.Number `json:"maxLeverage"`
		} `json:"data"`
	}
	if err := json.Unmarshal(raw, &env); err != nil {
		return instrumentInfo{}, errInternal("parse contract detail", err)
	}
	if env.Code != 0 {
		return instrumentInfo{}, &trade.Error{
			Kind:    trade.KindUser,
			Message: fmt.Sprintf("symbol %s not listed on MEXC", sym),
		}
	}
	minVol, _ := env.Data.MinVol.Int64()
	volUnit, _ := env.Data.VolUnit.Int64()
	csz, _ := env.Data.ContractSize.Float64()
	mlv, _ := env.Data.MaxLeverage.Int64()
	out := instrumentInfo{
		MinVol:       minVol,
		VolUnit:      volUnit,
		ContractSize: csz,
		MaxLeverage:  int(mlv),
		At:           time.Now(),
	}
	if out.VolUnit <= 0 {
		out.VolUnit = 1
	}
	if out.MinVol <= 0 {
		out.MinVol = 1
	}
	if out.ContractSize <= 0 {
		out.ContractSize = 1
	}
	a.infoMu.Lock()
	a.info[sym] = out
	a.infoMu.Unlock()
	return out, nil
}

// ── Quantity helpers ─────────────────────────────────────────────────────

func coinsToContracts(qty, contractSize float64, volUnit int64) int64 {
	if contractSize <= 0 {
		contractSize = 1
	}
	n := int64(math.Floor(qty / contractSize))
	if volUnit > 1 {
		n = (n / volUnit) * volUnit
	}
	if n < 0 {
		n = 0
	}
	return n
}

// ── Adapter methods ──────────────────────────────────────────────────────

func (a *Adapter) GetBalance(ctx context.Context, creds trade.Creds) (*trade.Balance, error) {
	body, err := a.signedRequest(ctx, creds, http.MethodGet,
		"/api/v1/private/account/assets", nil, nil)
	if err != nil {
		return nil, err
	}
	var rows []struct {
		Currency         string      `json:"currency"`
		AvailableBalance json.Number `json:"availableBalance"`
		Equity           json.Number `json:"equity"`
	}
	if err := json.Unmarshal(body, &rows); err != nil {
		return nil, errInternal("parse balance", err)
	}
	for _, r := range rows {
		if r.Currency != "USDT" {
			continue
		}
		avail, _ := r.AvailableBalance.Float64()
		total, _ := r.Equity.Float64()
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
	openType := 1 // isolated
	if req.MarginMode == trade.MarginCross {
		openType = 2
	}
	_, err := a.signedRequest(ctx, creds, http.MethodPost,
		"/api/v1/private/position/change_leverage", nil, map[string]any{
			"symbol":   toMexcSymbol(req.Symbol),
			"leverage": req.Leverage,
			"openType": openType,
		})
	if err != nil {
		te, ok := err.(*trade.Error)
		if ok && (strings.Contains(strings.ToLower(te.Message), "no need") ||
			strings.Contains(strings.ToLower(te.Message), "not changed")) {
			return nil
		}
	}
	return err
}

func (a *Adapter) PlaceOrder(ctx context.Context, creds trade.Creds, req trade.OpenRequest) (*trade.Result, error) {
	if err := req.Validate(); err != nil {
		return nil, err
	}
	sym := toMexcSymbol(req.Symbol)
	info, err := a.instrumentInfo(ctx, sym)
	if err != nil {
		return nil, err
	}
	vol := coinsToContracts(req.Quantity, info.ContractSize, info.VolUnit)
	if vol < info.MinVol {
		return nil, errUser("vol below MEXC minimum (%d contracts × %g %s each)",
			info.MinVol, info.ContractSize, req.Symbol)
	}
	mexcSide := 1 // open_long
	if req.Side == trade.SideSell {
		mexcSide = 3 // open_short
	}
	openType := 1 // isolated
	if req.MarginMode == trade.MarginCross {
		openType = 2
	}
	body, err := a.signedRequest(ctx, creds, http.MethodPost,
		"/api/v1/private/order/submit", nil, map[string]any{
			"symbol":   sym,
			"price":    "0",
			"vol":      vol,
			"side":     mexcSide,
			"type":     5, // market
			"openType": openType,
		})
	if err != nil {
		return nil, err
	}
	// MEXC's "data" can be a bare orderId string OR {orderId: ...}.
	orderID := ""
	{
		var s string
		if err := json.Unmarshal(body, &s); err == nil && s != "" {
			orderID = s
		} else {
			var obj struct {
				OrderID json.Number `json:"orderId"`
			}
			_ = json.Unmarshal(body, &obj)
			orderID = string(obj.OrderID)
		}
	}
	return &trade.Result{
		OrderID:   orderID,
		Symbol:    req.Symbol,
		Side:      req.Side,
		Quantity:  float64(vol) * info.ContractSize,
		Status:    "NEW",
		CreatedAt: time.Now().UTC(),
		Raw:       body,
	}, nil
}

func (a *Adapter) ClosePosition(ctx context.Context, creds trade.Creds, req trade.CloseRequest) (*trade.Result, error) {
	if req.Symbol == "" {
		return nil, errUser("symbol required")
	}
	sym := toMexcSymbol(req.Symbol)
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
	info, err := a.instrumentInfo(ctx, sym)
	if err != nil {
		// Fall back to qty=1 contract — MEXC rounds down internally.
		info = instrumentInfo{ContractSize: 1, VolUnit: 1, MinVol: 1}
	}
	vol := coinsToContracts(p.Quantity, info.ContractSize, info.VolUnit)
	if vol <= 0 {
		vol = 1
	}
	mexcSide := 4 // close_long
	if p.Side == trade.SideSell {
		mexcSide = 2 // close_short
	}
	openType := 1 // isolated
	if p.MarginMode == trade.MarginCross {
		openType = 2
	}
	body, err := a.signedRequest(ctx, creds, http.MethodPost,
		"/api/v1/private/order/submit", nil, map[string]any{
			"symbol":   sym,
			"price":    "0",
			"vol":      vol,
			"side":     mexcSide,
			"type":     5,
			"openType": openType,
		})
	if err != nil {
		return nil, err
	}
	orderID := ""
	{
		var s string
		if err := json.Unmarshal(body, &s); err == nil && s != "" {
			orderID = s
		} else {
			var obj struct {
				OrderID json.Number `json:"orderId"`
			}
			_ = json.Unmarshal(body, &obj)
			orderID = string(obj.OrderID)
		}
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
	params := map[string]string{}
	if symbol != "" {
		params["symbol"] = toMexcSymbol(symbol)
	}
	body, err := a.signedRequest(ctx, creds, http.MethodGet,
		"/api/v1/private/position/open_positions", params, nil)
	if err != nil {
		return nil, err
	}
	var rows []struct {
		Symbol         string      `json:"symbol"`
		HoldVol        json.Number `json:"holdVol"`
		PositionType   int         `json:"positionType"` // 1=long, 2=short
		OpenAvgPrice   json.Number `json:"openAvgPrice"`
		MarkPrice      json.Number `json:"markPrice"`
		UnrealisedPnl  json.Number `json:"unrealisedPnl"`
		Leverage       json.Number `json:"leverage"`
		ContractSize   json.Number `json:"contractSize"`
		OpenType       int         `json:"openType"` // 1=isolated, 2=cross
	}
	if err := json.Unmarshal(body, &rows); err != nil {
		return nil, errInternal("parse positions", err)
	}
	out := make([]trade.Position, 0, len(rows))
	for _, p := range rows {
		vol, _ := p.HoldVol.Float64()
		if vol == 0 {
			continue
		}
		side := trade.SideBuy
		if p.PositionType == 2 {
			side = trade.SideSell
		}
		mode := trade.MarginIsolated
		if p.OpenType == 2 {
			mode = trade.MarginCross
		}
		csz, _ := p.ContractSize.Float64()
		if csz <= 0 {
			csz = 1
		}
		entry, _ := p.OpenAvgPrice.Float64()
		mark, _ := p.MarkPrice.Float64()
		upl, _ := p.UnrealisedPnl.Float64()
		lev, _ := p.Leverage.Float64()
		stripped := strings.TrimSuffix(p.Symbol, "_USDT")
		out = append(out, trade.Position{
			Symbol:        stripped,
			Side:          side,
			Quantity:      vol * csz,
			EntryPrice:    entry,
			MarkPrice:     mark,
			Leverage:      int(lev),
			UnrealizedPnL: upl,
			Notional:      vol * csz * mark,
			MarginMode:    mode,
		})
	}
	return out, nil
}

// ── Friendly error mapping ───────────────────────────────────────────────

var friendlyMap = map[string]string{
	"2027":  "API key permissions insufficient.",
	"2028":  "Invalid API key.",
	"2029":  "Invalid signature.",
	"2030":  "Timestamp expired — clock skew.",
	"10001": "Insufficient margin balance.",
	"10004": "Insufficient available balance.",
	"10021": "Order quantity below minimum.",
	"10031": "Order qty exceeds position max.",
	"10072": "Symbol not enabled for trading.",
	"10219": "MEXC API in maintenance — try again shortly.",
	"510":   "Rate limit exceeded — try again in a moment.",
}

func friendly(code, msg string) string {
	if v, ok := friendlyMap[code]; ok {
		return v
	}
	if msg != "" {
		return msg
	}
	return "MEXC rejected the request."
}

func errUser(msg string, args ...any) *trade.Error {
	return &trade.Error{Kind: trade.KindUser, Message: fmt.Sprintf(msg, args...)}
}

func errInternal(msg string, cause error) *trade.Error {
	return &trade.Error{Kind: trade.KindInternal, Message: msg, Cause: cause}
}

var _ trade.Adapter = (*Adapter)(nil)
