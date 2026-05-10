// BingX USDT-M Swap trade adapter.
//
// Port of `backend/services/trade_adapters/bingx.py`.
//
// Signing: HMAC-SHA256 hex of the URL-encoded sorted query string.
// Auth header: X-BX-APIKEY. Signature appended to URL as `&signature=…`.
//
// Quirks:
//   - Symbol form: "BTC-USDT" (dash-separated).
//   - Quantity in coins, rounded to `stepSize` and `quantityPrecision`
//     from /openApi/swap/v2/quote/contracts (cached 10 min).
//   - Hedge-mode awareness: `positionSide=LONG/SHORT` on order body.
//     Try `BOTH` first on set-leverage (one-way mode); on hedge-mode
//     error, set LONG and SHORT separately.
//   - Close: BingX rejects `reduceOnly` in hedge mode (the positionSide
//     already disambiguates). Adapter sends reduceOnly only in one-way.
package bingx

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

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

const baseURL = "https://open-api.bingx.com"

type Adapter struct {
	httpClient *http.Client

	infoMu     sync.RWMutex
	info       map[string]symbolInfo
	infoLoaded time.Time
}

type symbolInfo struct {
	StepSize          float64
	MinQty            float64
	QuantityPrecision int
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
		info: make(map[string]symbolInfo, 256),
	}
}

func init() {
	a := New()
	trade.Register("bingx", a)
	go func() {
		time.Sleep(2 * time.Second)
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_, _ = a.loadContracts(ctx)
	}()
}

func (a *Adapter) Name() string { return "bingx" }

func toBingXSymbol(sym string) string { return strings.ToUpper(sym) + "-USDT" }

// ── Signing ──────────────────────────────────────────────────────────────

// signedQuery builds the canonical query string used for both signing
// AND the URL-on-the-wire. We MUST use exactly the bytes we hashed —
// passing params via http.Request.URL.RawQuery (alphabetic) would
// re-encode and break the signature.
func signedQuery(params map[string]string, secret string) string {
	q := trade.SortedFormQuery(params)
	sig := trade.HMACHexSHA256(secret, q)
	return q + "&signature=" + sig
}

func (a *Adapter) signedRequest(
	ctx context.Context, creds trade.Creds, method, path string,
	params map[string]string,
) (json.RawMessage, error) {
	if params == nil {
		params = map[string]string{}
	}
	params["timestamp"] = strconv.FormatInt(time.Now().UnixMilli(), 10)
	q := signedQuery(params, creds.APISecret)

	url := baseURL + path + "?" + q
	req, err := http.NewRequestWithContext(ctx, method, url, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("X-BX-APIKEY", creds.APIKey)
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, &trade.Error{Kind: trade.KindTransient, Message: err.Error(), Cause: err}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)

	var env struct {
		Code int             `json:"code"`
		Msg  string          `json:"msg"`
		Data json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(raw, &env); err != nil {
		if resp.StatusCode >= 400 {
			return nil, &trade.Error{
				Kind:    trade.KindExchange,
				Message: fmt.Sprintf("BingX HTTP %d: %s", resp.StatusCode, string(raw)),
			}
		}
		return nil, errInternal("parse envelope", err)
	}
	if env.Code != 0 && env.Code != 200 {
		codeStr := strconv.Itoa(env.Code)
		msg := friendly(codeStr, env.Msg)
		if resp.StatusCode == 429 || env.Code == 100410 {
			return nil, &trade.Error{Kind: trade.KindRateLimit, Code: codeStr, Message: msg}
		}
		return nil, &trade.Error{Kind: trade.KindExchange, Code: codeStr, Message: msg}
	}
	return env.Data, nil
}

// ── Instrument cache ─────────────────────────────────────────────────────

func (a *Adapter) loadContracts(ctx context.Context) (map[string]symbolInfo, error) {
	a.infoMu.RLock()
	cached := a.info
	loadedAt := a.infoLoaded
	a.infoMu.RUnlock()
	if !loadedAt.IsZero() && time.Since(loadedAt) < infoTTL {
		return cached, nil
	}
	u := baseURL + "/openApi/swap/v2/quote/contracts"
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	resp, err := a.httpClient.Do(req)
	if err != nil {
		if cached != nil {
			return cached, nil
		}
		return nil, &trade.Error{Kind: trade.KindTransient, Message: err.Error()}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var doc struct {
		Data []struct {
			Symbol            string      `json:"symbol"`
			Status            int         `json:"status"`
			TradeMinQuantity  json.Number `json:"tradeMinQuantity"`
			QuantityPrecision json.Number `json:"quantityPrecision"`
			Size              json.Number `json:"size"` // step size in some BingX responses
		} `json:"data"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		return nil, errInternal("parse contracts", err)
	}
	out := make(map[string]symbolInfo, len(doc.Data))
	for _, c := range doc.Data {
		if !strings.HasSuffix(c.Symbol, "-USDT") {
			continue
		}
		minQ, _ := c.TradeMinQuantity.Float64()
		prec, _ := c.QuantityPrecision.Int64()
		step, _ := c.Size.Float64()
		out[c.Symbol] = symbolInfo{
			StepSize:          step,
			MinQty:            minQ,
			QuantityPrecision: int(prec),
		}
	}
	a.infoMu.Lock()
	a.info = out
	a.infoLoaded = time.Now()
	a.infoMu.Unlock()
	return out, nil
}

// ── Quantity helpers ─────────────────────────────────────────────────────

func roundQty(qty, step float64, prec int) float64 {
	if step > 0 {
		qty = math.Floor(qty/step) * step
	}
	if prec < 0 {
		prec = 0
	}
	factor := math.Pow10(prec)
	return math.Floor(qty*factor) / factor
}

func qtyString(q float64, prec int) string {
	if prec < 0 {
		prec = 0
	}
	s := strconv.FormatFloat(q, 'f', prec, 64)
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
		"/openApi/swap/v2/user/balance", nil)
	if err != nil {
		return nil, err
	}
	// BingX's "data" can be {balance: {...}} OR the balance object directly.
	var asObj struct {
		Balance struct {
			AvailableMargin json.Number `json:"availableMargin"`
			Equity          json.Number `json:"equity"`
		} `json:"balance"`
	}
	_ = json.Unmarshal(body, &asObj)
	avail, _ := asObj.Balance.AvailableMargin.Float64()
	total, _ := asObj.Balance.Equity.Float64()
	if avail == 0 && total == 0 {
		var direct struct {
			AvailableMargin json.Number `json:"availableMargin"`
			Equity          json.Number `json:"equity"`
		}
		_ = json.Unmarshal(body, &direct)
		avail, _ = direct.AvailableMargin.Float64()
		total, _ = direct.Equity.Float64()
	}
	if total == 0 {
		total = avail
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
	sym := toBingXSymbol(req.Symbol)
	mt := "ISOLATED"
	if req.MarginMode == trade.MarginCross {
		mt = "CROSSED"
	}

	g, gctx := errgroup.WithContext(ctx)

	g.Go(func() error {
		_, err := a.signedRequest(gctx, creds, http.MethodPost,
			"/openApi/swap/v2/trade/marginType", map[string]string{
				"symbol": sym, "marginType": mt,
			})
		// Already-set / not-modified — non-fatal.
		if err != nil {
			te, ok := err.(*trade.Error)
			if ok && (strings.Contains(strings.ToLower(te.Message), "no need") ||
				strings.Contains(strings.ToLower(te.Message), "not modified")) {
				return nil
			}
			return err
		}
		return nil
	})

	g.Go(func() error {
		// Try BOTH (one-way mode) first.
		_, err := a.signedRequest(gctx, creds, http.MethodPost,
			"/openApi/swap/v2/trade/leverage", map[string]string{
				"symbol":   sym,
				"side":     "BOTH",
				"leverage": strconv.Itoa(req.Leverage),
			})
		if err == nil {
			return nil
		}
		te, ok := err.(*trade.Error)
		if !ok {
			return err
		}
		// 80012 / 109400 — leverage already set, non-fatal.
		if te.Code == "80012" || te.Code == "109400" || te.Code == "100413" {
			return nil
		}
		// Hedge-mode: BingX returns "side error" / 100400 → set LONG + SHORT
		// separately, both non-fatal individually.
		ms := strings.ToLower(te.Message)
		if te.Code == "100400" || strings.Contains(ms, "hedge") || strings.Contains(ms, "side") {
			for _, s := range []string{"LONG", "SHORT"} {
				_, _ = a.signedRequest(gctx, creds, http.MethodPost,
					"/openApi/swap/v2/trade/leverage", map[string]string{
						"symbol":   sym,
						"side":     s,
						"leverage": strconv.Itoa(req.Leverage),
					})
			}
			return nil
		}
		return err
	})

	return g.Wait()
}

func (a *Adapter) PlaceOrder(ctx context.Context, creds trade.Creds, req trade.OpenRequest) (*trade.Result, error) {
	if err := req.Validate(); err != nil {
		return nil, err
	}
	sym := toBingXSymbol(req.Symbol)
	contracts, err := a.loadContracts(ctx)
	if err == nil {
		if info, ok := contracts[sym]; ok {
			qty := roundQty(req.Quantity, info.StepSize, info.QuantityPrecision)
			if qty <= 0 || (info.MinQty > 0 && qty < info.MinQty) {
				return nil, errUser("quantity below BingX minimum (%g %s)", info.MinQty, req.Symbol)
			}
			req.Quantity = qty
		}
	}

	posSide := "LONG"
	side := "BUY"
	if req.Side == trade.SideSell {
		posSide = "SHORT"
		side = "SELL"
	}
	prec := 8
	if info, ok := contracts[sym]; ok {
		prec = info.QuantityPrecision
	}
	orderParams := map[string]string{
		"symbol":       sym,
		"side":         side,
		"positionSide": posSide,
		"quantity":     qtyString(req.Quantity, prec),
	}
	switch req.OrderType {
	case trade.OrderLimit:
		orderParams["type"] = "LIMIT"
		orderParams["price"] = strconv.FormatFloat(req.LimitPrice, 'f', -1, 64)
	case trade.OrderStopMarket:
		orderParams["type"] = "STOP_MARKET"
		orderParams["stopPrice"] = strconv.FormatFloat(req.StopPrice, 'f', -1, 64)
	case trade.OrderTakeProfitMkt:
		orderParams["type"] = "TAKE_PROFIT_MARKET"
		orderParams["stopPrice"] = strconv.FormatFloat(req.StopPrice, 'f', -1, 64)
	default:
		orderParams["type"] = "MARKET"
	}
	body, err := a.signedRequest(ctx, creds, http.MethodPost,
		"/openApi/swap/v2/trade/order", orderParams)
	if err != nil {
		return nil, err
	}
	var resp struct {
		Order struct {
			OrderID  json.Number `json:"orderId"`
			AvgPrice json.Number `json:"avgPrice"`
			Status   string      `json:"status"`
		} `json:"order"`
	}
	_ = json.Unmarshal(body, &resp)
	avg, _ := resp.Order.AvgPrice.Float64()
	return &trade.Result{
		OrderID:   string(resp.Order.OrderID),
		Symbol:    req.Symbol,
		Side:      req.Side,
		Quantity:  req.Quantity,
		AvgPrice:  avg,
		Status:    resp.Order.Status,
		CreatedAt: time.Now().UTC(),
		Raw:       body,
	}, nil
}

func (a *Adapter) ClosePosition(ctx context.Context, creds trade.Creds, req trade.CloseRequest) (*trade.Result, error) {
	if req.Symbol == "" {
		return nil, errUser("symbol required")
	}
	sym := toBingXSymbol(req.Symbol)
	body, err := a.signedRequest(ctx, creds, http.MethodGet,
		"/openApi/swap/v2/user/positions", map[string]string{"symbol": sym})
	if err != nil {
		return nil, err
	}
	var rows []struct {
		Symbol       string      `json:"symbol"`
		PositionAmt  json.Number `json:"positionAmt"`
		AvailableAmt json.Number `json:"availableAmt"`
		PositionSide string      `json:"positionSide"`
	}
	_ = json.Unmarshal(body, &rows)
	wantPside := "LONG"
	if req.Side == trade.SideSell {
		wantPside = "SHORT"
	}
	var (
		amt           float64
		positionSide  = "BOTH"
	)
	for _, p := range rows {
		v, _ := p.PositionAmt.Float64()
		if v == 0 {
			v, _ = p.AvailableAmt.Float64()
		}
		if v == 0 {
			continue
		}
		ps := strings.ToUpper(p.PositionSide)
		if ps == wantPside {
			amt = v
			positionSide = ps
			break
		}
	}
	if amt == 0 {
		// Fallback: any non-zero leg (one-way / BOTH).
		for _, p := range rows {
			v, _ := p.PositionAmt.Float64()
			if v == 0 {
				v, _ = p.AvailableAmt.Float64()
			}
			if v == 0 {
				continue
			}
			amt = v
			positionSide = strings.ToUpper(p.PositionSide)
			if positionSide == "" {
				positionSide = "BOTH"
			}
			break
		}
	}
	if amt == 0 {
		return &trade.Result{Symbol: req.Symbol, Status: "FLAT"}, nil
	}

	reduceSide := "SELL"
	switch positionSide {
	case "LONG":
		reduceSide = "SELL"
	case "SHORT":
		reduceSide = "BUY"
	default:
		if amt < 0 {
			reduceSide = "BUY"
		}
	}

	prec := 8
	if c, _ := a.loadContracts(ctx); c != nil {
		if info, ok := c[sym]; ok {
			prec = info.QuantityPrecision
		}
	}
	bodyParams := map[string]string{
		"symbol":       sym,
		"type":         "MARKET",
		"side":         reduceSide,
		"positionSide": positionSide,
		"quantity":     qtyString(math.Abs(amt), prec),
	}
	if positionSide == "BOTH" {
		bodyParams["reduceOnly"] = "true"
	}
	resp, err := a.signedRequest(ctx, creds, http.MethodPost,
		"/openApi/swap/v2/trade/order", bodyParams)
	if err != nil {
		return nil, err
	}
	var out struct {
		Order struct {
			OrderID json.Number `json:"orderId"`
		} `json:"order"`
	}
	_ = json.Unmarshal(resp, &out)
	closeSide := trade.SideSell
	if reduceSide == "BUY" {
		closeSide = trade.SideBuy
	}
	return &trade.Result{
		OrderID:   string(out.Order.OrderID),
		Symbol:    req.Symbol,
		Side:      closeSide,
		Quantity:  math.Abs(amt),
		Status:    "NEW",
		CreatedAt: time.Now().UTC(),
		Raw:       resp,
	}, nil
}

func (a *Adapter) ListPositions(ctx context.Context, creds trade.Creds, symbol string) ([]trade.Position, error) {
	params := map[string]string{}
	if symbol != "" {
		params["symbol"] = toBingXSymbol(symbol)
	}
	body, err := a.signedRequest(ctx, creds, http.MethodGet,
		"/openApi/swap/v2/user/positions", params)
	if err != nil {
		return nil, err
	}
	var rows []struct {
		Symbol        string      `json:"symbol"`
		PositionAmt   json.Number `json:"positionAmt"`
		AvgPrice      json.Number `json:"avgPrice"`
		MarkPrice     json.Number `json:"markPrice"`
		Leverage      json.Number `json:"leverage"`
		UnrealizedPnL json.Number `json:"unrealizedProfit"`
		PositionSide  string      `json:"positionSide"`
	}
	if err := json.Unmarshal(body, &rows); err != nil {
		return nil, errInternal("parse positions", err)
	}
	out := make([]trade.Position, 0, len(rows))
	for _, p := range rows {
		amt, _ := p.PositionAmt.Float64()
		if amt == 0 {
			continue
		}
		side := trade.SideBuy
		ps := strings.ToUpper(p.PositionSide)
		if ps == "SHORT" || (ps == "BOTH" && amt < 0) {
			side = trade.SideSell
		}
		entry, _ := p.AvgPrice.Float64()
		mark, _ := p.MarkPrice.Float64()
		lev, _ := p.Leverage.Float64()
		upl, _ := p.UnrealizedPnL.Float64()
		stripped := strings.TrimSuffix(p.Symbol, "-USDT")
		out = append(out, trade.Position{
			Symbol:        stripped,
			Side:          side,
			Quantity:      math.Abs(amt),
			EntryPrice:    entry,
			MarkPrice:     mark,
			Leverage:      int(lev),
			UnrealizedPnL: upl,
			Notional:      math.Abs(amt) * mark,
		})
	}
	return out, nil
}

// ── Friendly error mapping ───────────────────────────────────────────────

var friendlyMap = map[string]string{
	"100400": "Position-side / margin-mode mismatch.",
	"100410": "Rate limit exceeded — try again in a moment.",
	"80012":  "Leverage already at requested value.",
	"100413": "Leverage not modified.",
	"109400": "Leverage not modified.",
	"110030": "Insufficient margin balance.",
	"103009": "Order qty below contract minimum.",
}

func friendly(code, msg string) string {
	if v, ok := friendlyMap[code]; ok {
		return v
	}
	if msg != "" {
		return msg
	}
	return "BingX rejected the request."
}

func errUser(msg string, args ...any) *trade.Error {
	return &trade.Error{Kind: trade.KindUser, Message: fmt.Sprintf(msg, args...)}
}

func errInternal(msg string, cause error) *trade.Error {
	return &trade.Error{Kind: trade.KindInternal, Message: msg, Cause: cause}
}

// Avoid unused-import lint when downstream refactors hit.
var _ = url.QueryEscape

var _ trade.Adapter = (*Adapter)(nil)
