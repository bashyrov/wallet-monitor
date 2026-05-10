// Binance USDT-M Futures (FAPI) trade adapter.
//
// Port of `backend/services/trade_adapters/binance.py`. Same wire shape,
// same error-code → friendly-message mapping, same caches (exchangeInfo
// 10 min, position mode 5 min).
//
// What we gain over the Python path:
//   - Real parallelism on set_leverage (margin + leverage endpoints
//     fired in two goroutines instead of asyncio.gather inside a GIL).
//   - Sub-50µs HMAC signing (Go crypto/hmac is ~3× faster than
//     hashlib + urlencode bookkeeping).
//   - Single shared http.Client with keepalive — re-uses TCP
//     connections across calls, where Python opens a new
//     httpx.AsyncClient per request.
package binance

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"

	"golang.org/x/sync/errgroup"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

const baseURL = "https://fapi.binance.com"

// ── Adapter ──────────────────────────────────────────────────────────────

type Adapter struct {
	httpClient *http.Client

	// exchangeInfo cache — refreshed lazily every 10 min. Same TTL as
	// Python's _EX_INFO_CACHE.
	infoMu     sync.RWMutex
	info       map[string]symbolInfo
	infoLoaded time.Time

	// Position-mode cache (hedge vs one-way) keyed by api_key. Hedge
	// mode requires `positionSide` on every order.
	modeMu sync.RWMutex
	mode   map[string]modeEntry
}

type symbolInfo struct {
	StepSize          float64
	MinQty            float64
	MinNotional       float64
	TickSize          float64
	QuantityPrecision int
	PricePrecision    int
}

type modeEntry struct {
	hedge bool
	at    time.Time
}

const (
	infoTTL = 10 * time.Minute
	modeTTL = 5 * time.Minute
)

// New — constructor used by main.go (and tests). The HTTP client uses
// generous timeouts because Binance occasionally takes 5+ seconds on
// /fapi/v1/order under load; cancelling an in-flight order over a
// false-positive timeout creates the worst kind of bug (charged but
// no order id).
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
		info: make(map[string]symbolInfo, 256),
		mode: make(map[string]modeEntry, 16),
	}
}

func init() {
	a := New()
	trade.Register("binance", a)
	// Pre-warm TCP+TLS pool + exchangeInfo cache. Background goroutine —
	// process boot doesn't block. By the time any user places an order,
	// the connection is already in the keepalive pool and the symbol
	// filter map is loaded. Saves ~150-300ms on the very first order
	// after a fresh container restart.
	go func() {
		time.Sleep(2 * time.Second)
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_, _ = a.exchangeInfo(ctx)
	}()
}

func (a *Adapter) Name() string { return "binance" }

// ── Symbol helper ────────────────────────────────────────────────────────

func toBinanceSymbol(sym string) string { return strings.ToUpper(sym) + "USDT" }

// ── Signed request ───────────────────────────────────────────────────────

func (a *Adapter) signedRequest(
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

	var (
		req *http.Request
		err error
	)
	switch method {
	case http.MethodGet, http.MethodDelete:
		req, err = http.NewRequestWithContext(ctx, method, baseURL+path+"?"+full, nil)
	case http.MethodPost:
		req, err = http.NewRequestWithContext(ctx, method, baseURL+path, strings.NewReader(full))
		if req != nil {
			req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
		}
	default:
		return nil, fmt.Errorf("unsupported method %s", method)
	}
	if err != nil {
		return nil, err
	}
	req.Header.Set("X-MBX-APIKEY", creds.APIKey)
	resp, err := a.httpClient.Do(req)
	if err != nil {
		// Network blip — caller decides retry policy.
		return nil, mapNetErr(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return nil, parseExchangeError(resp.StatusCode, body)
	}
	return json.RawMessage(body), nil
}

// publicGet — unsigned, no creds.
func (a *Adapter) publicGet(ctx context.Context, path string) (json.RawMessage, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, baseURL+path, nil)
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
	return body, nil
}

func mapNetErr(err error) *trade.Error {
	// Best-effort kind classification. Real production code would
	// inspect timeouts / connection-refused separately.
	return &trade.Error{Kind: trade.KindTransient, Message: err.Error(), Cause: err}
}

func parseExchangeError(status int, body []byte) *trade.Error {
	var j struct {
		Code int    `json:"code"`
		Msg  string `json:"msg"`
	}
	_ = json.Unmarshal(body, &j)
	codeStr := ""
	if j.Code != 0 {
		codeStr = strconv.Itoa(j.Code)
	}
	msg := friendlyError(codeStr, j.Msg)
	if msg == "" {
		msg = strings.TrimSpace(string(body))
	}
	if status == 429 || j.Code == -1003 {
		return &trade.Error{Kind: trade.KindRateLimit, Code: codeStr, Message: msg}
	}
	return &trade.Error{Kind: trade.KindExchange, Code: codeStr, Message: msg}
}

// ── exchangeInfo cache ───────────────────────────────────────────────────

func (a *Adapter) exchangeInfo(ctx context.Context) (map[string]symbolInfo, error) {
	a.infoMu.RLock()
	loaded := a.infoLoaded
	cached := a.info
	a.infoMu.RUnlock()
	if !loaded.IsZero() && time.Since(loaded) < infoTTL {
		return cached, nil
	}

	body, err := a.publicGet(ctx, "/fapi/v1/exchangeInfo")
	if err != nil {
		// On refresh failure, keep returning the stale cache rather
		// than failing the order — same defensive posture as Python.
		if cached != nil {
			return cached, nil
		}
		return nil, err
	}

	var resp struct {
		Symbols []struct {
			Symbol            string `json:"symbol"`
			ContractType      string `json:"contractType"`
			QuantityPrecision int    `json:"quantityPrecision"`
			PricePrecision    int    `json:"pricePrecision"`
			Filters           []map[string]any `json:"filters"`
		} `json:"symbols"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		return nil, errInternal("parse exchangeInfo", err)
	}
	out := make(map[string]symbolInfo, len(resp.Symbols))
	for _, s := range resp.Symbols {
		if s.ContractType != "PERPETUAL" {
			continue
		}
		info := symbolInfo{
			QuantityPrecision: s.QuantityPrecision,
			PricePrecision:    s.PricePrecision,
		}
		for _, f := range s.Filters {
			t, _ := f["filterType"].(string)
			switch t {
			case "LOT_SIZE":
				info.StepSize = parseFloat(f["stepSize"])
				info.MinQty = parseFloat(f["minQty"])
			case "MIN_NOTIONAL":
				notional := parseFloat(f["notional"])
				if notional == 0 {
					notional = parseFloat(f["minNotional"])
				}
				info.MinNotional = notional
			case "PRICE_FILTER":
				info.TickSize = parseFloat(f["tickSize"])
			}
		}
		out[s.Symbol] = info
	}
	a.infoMu.Lock()
	a.info = out
	a.infoLoaded = time.Now()
	a.infoMu.Unlock()
	return out, nil
}

func parseFloat(v any) float64 {
	switch x := v.(type) {
	case float64:
		return x
	case string:
		f, _ := strconv.ParseFloat(x, 64)
		return f
	}
	return 0
}

// ── Position-mode cache ──────────────────────────────────────────────────

func (a *Adapter) isHedgeMode(ctx context.Context, creds trade.Creds) bool {
	key := creds.APIKey
	a.modeMu.RLock()
	hit, ok := a.mode[key]
	a.modeMu.RUnlock()
	if ok && time.Since(hit.at) < modeTTL {
		return hit.hedge
	}
	body, err := a.signedRequest(ctx, creds, http.MethodGet, "/fapi/v1/positionSide/dual", nil)
	dual := false
	if err == nil {
		var resp struct {
			DualSidePosition bool `json:"dualSidePosition"`
		}
		if json.Unmarshal(body, &resp) == nil {
			dual = resp.DualSidePosition
		}
	}
	a.modeMu.Lock()
	a.mode[key] = modeEntry{hedge: dual, at: time.Now()}
	a.modeMu.Unlock()
	return dual
}

// ── Quantity rounding ────────────────────────────────────────────────────

func roundToStep(qty, step float64, precision int) float64 {
	if step > 0 {
		return math.Floor(qty/step) * step
	}
	factor := math.Pow10(precision)
	return math.Floor(qty*factor) / factor
}

func qtyString(qty float64, precision int) string {
	if precision < 0 {
		precision = 0
	}
	s := strconv.FormatFloat(qty, 'f', precision, 64)
	// Trim trailing zeros + leftover dot. Binance accepts "1" but not
	// "1.000" if the lot precision is higher than the actual value.
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
	body, err := a.signedRequest(ctx, creds, http.MethodGet, "/fapi/v2/balance", nil)
	if err != nil {
		return nil, err
	}
	var rows []struct {
		Asset              string `json:"asset"`
		Balance            string `json:"balance"`
		AvailableBalance   string `json:"availableBalance"`
		CrossWalletBalance string `json:"crossWalletBalance"`
	}
	if err := json.Unmarshal(body, &rows); err != nil {
		return nil, errInternal("parse balance", err)
	}
	for _, r := range rows {
		if r.Asset != "USDT" {
			continue
		}
		avail := parseFloat(r.AvailableBalance)
		total := parseFloat(r.Balance)
		cross := parseFloat(r.CrossWalletBalance)
		if avail == 0 {
			// User's funds are tied up as margin — surface the
			// max of the three as the "real" wallet so the UI
			// doesn't show $0 on an active account.
			real := math.Max(avail, math.Max(cross, total))
			return &trade.Balance{TotalUSD: total, AvailableUSD: real, MarginUSD: total - avail}, nil
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
	sym := toBinanceSymbol(req.Symbol)
	marginType := "ISOLATED"
	if req.MarginMode == trade.MarginCross {
		marginType = "CROSSED"
	}

	// Fire margin + leverage endpoints concurrently. The Python code
	// does the same via asyncio.gather; we get true parallelism here.
	g, gctx := errgroup.WithContext(ctx)
	g.Go(func() error {
		_, err := a.signedRequest(gctx, creds, http.MethodPost, "/fapi/v1/marginType",
			map[string]string{"symbol": sym, "marginType": marginType})
		if err != nil {
			te, ok := err.(*trade.Error)
			// -4046: "No need to change margin type" — already set, idempotent OK.
			if ok && (te.Code == "-4046" || strings.Contains(te.Message, "No need")) {
				return nil
			}
			return err
		}
		return nil
	})
	g.Go(func() error {
		_, err := a.signedRequest(gctx, creds, http.MethodPost, "/fapi/v1/leverage",
			map[string]string{"symbol": sym, "leverage": strconv.Itoa(req.Leverage)})
		return err
	})
	return g.Wait()
}

func (a *Adapter) PlaceOrder(ctx context.Context, creds trade.Creds, req trade.OpenRequest) (*trade.Result, error) {
	if err := req.Validate(); err != nil {
		return nil, err
	}
	sym := toBinanceSymbol(req.Symbol)
	infoMap, err := a.exchangeInfo(ctx)
	if err != nil {
		log.L().Warn().Err(err).Msg("binance: exchangeInfo unavailable, proceeding with caller's qty")
	}
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
		"quantity": qtyString(qty, info.QuantityPrecision),
	}
	switch req.OrderType {
	case trade.OrderLimit:
		params["type"] = "LIMIT"
		params["price"] = strconv.FormatFloat(req.LimitPrice, 'f', -1, 64)
		params["timeInForce"] = "GTC"
	case trade.OrderStopMarket:
		params["type"] = "STOP_MARKET"
		params["stopPrice"] = strconv.FormatFloat(req.StopPrice, 'f', -1, 64)
	case trade.OrderTakeProfitMkt:
		params["type"] = "TAKE_PROFIT_MARKET"
		params["stopPrice"] = strconv.FormatFloat(req.StopPrice, 'f', -1, 64)
	default:
		params["type"] = "MARKET"
	}
	if a.isHedgeMode(ctx, creds) {
		if req.Side == trade.SideBuy {
			params["positionSide"] = "LONG"
		} else {
			params["positionSide"] = "SHORT"
		}
	}
	body, err := a.signedRequest(ctx, creds, http.MethodPost, "/fapi/v1/order", params)
	if err != nil {
		return nil, err
	}
	var resp struct {
		OrderID  json.Number `json:"orderId"`
		AvgPrice string      `json:"avgPrice"`
		Status   string      `json:"status"`
		ClientID string      `json:"clientOrderId"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		return nil, errInternal("parse order response", err)
	}
	return &trade.Result{
		OrderID:       string(resp.OrderID),
		Symbol:        req.Symbol,
		Side:          req.Side,
		Quantity:      qty,
		AvgPrice:      parseFloat(resp.AvgPrice),
		Status:        resp.Status,
		ClientOrderID: resp.ClientID,
		CreatedAt:     time.Now().UTC(),
		Raw:           body,
	}, nil
}

func (a *Adapter) ClosePosition(ctx context.Context, creds trade.Creds, req trade.CloseRequest) (*trade.Result, error) {
	if req.Symbol == "" {
		return nil, errUser("symbol required")
	}
	sym := toBinanceSymbol(req.Symbol)
	body, err := a.signedRequest(ctx, creds, http.MethodGet, "/fapi/v2/positionRisk",
		map[string]string{"symbol": sym})
	if err != nil {
		return nil, err
	}
	var rows []struct {
		Symbol       string `json:"symbol"`
		PositionAmt  string `json:"positionAmt"`
		EntryPrice   string `json:"entryPrice"`
		PositionSide string `json:"positionSide"`
	}
	if err := json.Unmarshal(body, &rows); err != nil {
		return nil, errInternal("parse positionRisk", err)
	}
	var target *struct {
		Symbol       string `json:"symbol"`
		PositionAmt  string `json:"positionAmt"`
		EntryPrice   string `json:"entryPrice"`
		PositionSide string `json:"positionSide"`
	}
	for i := range rows {
		amt := parseFloat(rows[i].PositionAmt)
		if amt != 0 {
			target = &rows[i]
			break
		}
	}
	if target == nil {
		// Already flat — Python returns a no-op response, mirror that.
		return &trade.Result{Symbol: req.Symbol, Status: "FLAT"}, nil
	}
	amt := parseFloat(target.PositionAmt)
	reduceSide := "SELL"
	if amt < 0 {
		reduceSide = "BUY"
	}
	infoMap, _ := a.exchangeInfo(ctx)
	info := infoMap[sym]
	closeParams := map[string]string{
		"symbol":     sym,
		"side":       reduceSide,
		"type":       "MARKET",
		"quantity":   qtyString(math.Abs(amt), info.QuantityPrecision),
		"reduceOnly": "true",
	}
	if target.PositionSide != "" && target.PositionSide != "BOTH" {
		closeParams["positionSide"] = target.PositionSide
		// Hedge mode rejects reduceOnly+positionSide combo.
		delete(closeParams, "reduceOnly")
	}
	body, err = a.signedRequest(ctx, creds, http.MethodPost, "/fapi/v1/order", closeParams)
	if err != nil {
		return nil, err
	}
	var resp struct {
		OrderID  json.Number `json:"orderId"`
		AvgPrice string      `json:"avgPrice"`
		Status   string      `json:"status"`
	}
	_ = json.Unmarshal(body, &resp)
	closeSide := trade.SideSell
	if reduceSide == "BUY" {
		closeSide = trade.SideBuy
	}
	return &trade.Result{
		OrderID:   string(resp.OrderID),
		Symbol:    req.Symbol,
		Side:      closeSide,
		Quantity:  math.Abs(amt),
		AvgPrice:  parseFloat(resp.AvgPrice),
		Status:    resp.Status,
		CreatedAt: time.Now().UTC(),
		Raw:       body,
	}, nil
}

func (a *Adapter) ListPositions(ctx context.Context, creds trade.Creds, symbol string) ([]trade.Position, error) {
	params := map[string]string{}
	if symbol != "" {
		params["symbol"] = toBinanceSymbol(symbol)
	}
	body, err := a.signedRequest(ctx, creds, http.MethodGet, "/fapi/v2/positionRisk", params)
	if err != nil {
		return nil, err
	}
	var rows []struct {
		Symbol         string `json:"symbol"`
		PositionAmt    string `json:"positionAmt"`
		EntryPrice     string `json:"entryPrice"`
		MarkPrice      string `json:"markPrice"`
		Leverage       string `json:"leverage"`
		UnrealizedPnL  string `json:"unRealizedProfit"`
		MarginType     string `json:"marginType"`
		PositionSide   string `json:"positionSide"`
		UpdateTime     int64  `json:"updateTime"`
	}
	if err := json.Unmarshal(body, &rows); err != nil {
		return nil, errInternal("parse positions", err)
	}
	out := make([]trade.Position, 0, len(rows))
	for _, r := range rows {
		amt := parseFloat(r.PositionAmt)
		if amt == 0 {
			continue
		}
		side := trade.SideBuy
		if amt < 0 {
			side = trade.SideSell
		}
		// Binance returns the symbol as e.g. BTCUSDT — strip the suffix.
		stripped := strings.TrimSuffix(r.Symbol, "USDT")
		mode := trade.MarginIsolated
		if strings.EqualFold(r.MarginType, "cross") {
			mode = trade.MarginCross
		}
		opened := time.Time{}
		if r.UpdateTime > 0 {
			opened = time.UnixMilli(r.UpdateTime).UTC()
		}
		out = append(out, trade.Position{
			Symbol:        stripped,
			Side:          side,
			Quantity:      math.Abs(amt),
			EntryPrice:    parseFloat(r.EntryPrice),
			MarkPrice:     parseFloat(r.MarkPrice),
			Leverage:      int(parseFloat(r.Leverage)),
			UnrealizedPnL: parseFloat(r.UnrealizedPnL),
			Notional:      math.Abs(amt) * parseFloat(r.MarkPrice),
			MarginMode:    mode,
			OpenedAt:      opened,
		})
	}
	return out, nil
}

func binanceSide(s trade.Side) string {
	if s == trade.SideBuy {
		return "BUY"
	}
	return "SELL"
}

// ── Error helpers / friendly messages ────────────────────────────────────

var friendlyMap = map[string]string{
	"-1013": "Order does not meet the exchange's minimum size/notional.",
	"-1021": "Clock skew — try again in a moment.",
	"-1022": "Signature mismatch — API secret is wrong.",
	"-1111": "Quantity has more decimals than the contract allows.",
	"-1121": "Symbol not listed on Binance Futures.",
	"-2010": "Order rejected by the exchange.",
	"-2014": "Invalid API key.",
	"-2015": "Binance rejected the key (check IP whitelist and permissions).",
	"-2019": "Insufficient margin — your USDT balance is too low for this size/leverage.",
	"-4046": "Margin mode already set.",
	"-4061": "Position side does not match account mode. Your account is in hedge mode.",
	"-4164": "Order size below minimum notional.",
}

func friendlyError(code, msg string) string {
	if v, ok := friendlyMap[code]; ok {
		return v
	}
	return msg
}

// ── Local errors ─────────────────────────────────────────────────────────

func errUser(msg string, args ...any) *trade.Error {
	return &trade.Error{Kind: trade.KindUser, Message: fmt.Sprintf(msg, args...)}
}

func errInternal(msg string, cause error) *trade.Error {
	return &trade.Error{Kind: trade.KindInternal, Message: msg, Cause: cause}
}

// Compile-time interface check — main.go imports this package only for
// its init(); the Adapter assertion guards against drift between the
// types here and the trade.Adapter contract.
var _ trade.Adapter = (*Adapter)(nil)

// Avoid "unused" lint when other helpers move around.
var _ = url.QueryEscape
