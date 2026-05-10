// OKX V5 USDT-Perp trade adapter.
//
// Port of `backend/services/trade_adapters/okx.py`.
//
// Signing: base64( HMAC_SHA256(secret, ts+method+path+body) ).
// Headers (every signed request):
//
//	OK-ACCESS-KEY:        <key>
//	OK-ACCESS-SIGN:       <base64 signature>
//	OK-ACCESS-TIMESTAMP:  ISO 8601 (millisecond precision, "...Z")
//	OK-ACCESS-PASSPHRASE: <passphrase>
//	Content-Type:         application/json
//
// Quirks:
//   - Symbols are `BTC-USDT-SWAP`. The user-input `BTC` is mapped via
//     toOKXSymbol().
//   - Quantity is in CONTRACTS, not coins. Each instrument has a
//     `ctVal` (contract face value in coin). qty_coins ÷ ctVal =
//     contracts. We round to the lot step.
//   - Position mode: we always set `long_short_mode` (hedge) so
//     `posSide` is required on every order. This matches the Python
//     adapter's behaviour.
//   - Close uses the dedicated /trade/close-position endpoint (cleaner
//     than reduce-only order — returns OK even on stale qty).
package okx

import (
	"bytes"
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

const baseURL = "https://www.okx.com"

type Adapter struct {
	httpClient *http.Client

	instMu  sync.RWMutex
	inst    map[string]instrument
	instAt  time.Time
}

type instrument struct {
	LotSize float64 // lotSz — round qty to multiples of this
	MinSize float64 // minSz — minimum order size in contracts
	TickSize float64
	CtVal    float64 // ctVal — coins per contract (BTC-USDT-SWAP = 0.01 BTC)
}

const instrumentsTTL = 10 * time.Minute

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
		inst: make(map[string]instrument, 256),
	}
}

func init() {
	a := New()
	trade.Register("okx", a)
	// Pre-warm TCP+TLS pool + instruments cache.
	go func() {
		time.Sleep(2 * time.Second)
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_, _ = a.instruments(ctx)
	}()
}

func (a *Adapter) Name() string { return "okx" }

// ── Symbol mapping ───────────────────────────────────────────────────────

func toOKXSymbol(sym string) string {
	return strings.ToUpper(sym) + "-USDT-SWAP"
}

// ── Signing ──────────────────────────────────────────────────────────────

func okxTimestamp() string {
	// ISO 8601 with millisecond precision and a trailing Z.
	now := time.Now().UTC()
	return now.Format("2006-01-02T15:04:05.000Z")
}

func (a *Adapter) signedRequest(
	ctx context.Context, creds trade.Creds, method, path string,
	body any,
) (json.RawMessage, error) {
	if creds.Passphrase == "" {
		return nil, errUser("OKX requires passphrase credential")
	}
	ts := okxTimestamp()

	var bodyStr string
	var bodyReader io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return nil, errInternal("marshal body", err)
		}
		bodyStr = string(b)
		bodyReader = bytes.NewReader(b)
	}
	signSrc := ts + strings.ToUpper(method) + path + bodyStr
	sig := trade.HMACBase64SHA256(creds.APISecret, signSrc)

	url := baseURL + path
	req, err := http.NewRequestWithContext(ctx, method, url, bodyReader)
	if err != nil {
		return nil, err
	}
	req.Header.Set("OK-ACCESS-KEY", creds.APIKey)
	req.Header.Set("OK-ACCESS-SIGN", sig)
	req.Header.Set("OK-ACCESS-TIMESTAMP", ts)
	req.Header.Set("OK-ACCESS-PASSPHRASE", creds.Passphrase)
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

	// OKX V5: 200 OK can still mean logical error (code != "0").
	var env struct {
		Code string          `json:"code"`
		Msg  string          `json:"msg"`
		Data json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(raw, &env); err != nil {
		return nil, errInternal("parse envelope", err)
	}
	if env.Code != "0" && env.Code != "" {
		return nil, parseError(resp.StatusCode, raw)
	}
	return env.Data, nil
}

func (a *Adapter) publicGet(ctx context.Context, path string) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, baseURL+path, nil)
	if err != nil {
		return nil, err
	}
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, &trade.Error{Kind: trade.KindTransient, Message: err.Error()}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return nil, parseError(resp.StatusCode, raw)
	}
	return raw, nil
}

func parseError(status int, body []byte) *trade.Error {
	var env struct {
		Code string `json:"code"`
		Msg  string `json:"msg"`
		Data []struct {
			SCode string `json:"sCode"`
			SMsg  string `json:"sMsg"`
		} `json:"data"`
	}
	_ = json.Unmarshal(body, &env)
	code := env.Code
	msg := env.Msg
	// Promote nested sCode/sMsg whenever it carries more info than the
	// top-level. OKX top-level code is often "1" (generic batch failure)
	// while the real reason lives in data[0].sCode (e.g. "51121").
	if len(env.Data) > 0 && env.Data[0].SCode != "" {
		code = env.Data[0].SCode
		if env.Data[0].SMsg != "" {
			msg = env.Data[0].SMsg
		}
	}
	if msg == "" {
		msg = strings.TrimSpace(string(body))
	}
	if status == 429 || code == "50011" || code == "50061" {
		return &trade.Error{Kind: trade.KindRateLimit, Code: code, Message: friendly(code, msg)}
	}
	return &trade.Error{Kind: trade.KindExchange, Code: code, Message: friendly(code, msg)}
}

// ── Instrument cache (contract size + lot size) ──────────────────────────

func (a *Adapter) instruments(ctx context.Context) (map[string]instrument, error) {
	a.instMu.RLock()
	cached := a.inst
	cachedAt := a.instAt
	a.instMu.RUnlock()
	if cached != nil && time.Since(cachedAt) < instrumentsTTL {
		return cached, nil
	}
	raw, err := a.publicGet(ctx, "/api/v5/public/instruments?instType=SWAP")
	if err != nil {
		if cached != nil {
			return cached, nil // stale fallback
		}
		return nil, err
	}
	var resp struct {
		Data []struct {
			InstID  string `json:"instId"`
			LotSz   string `json:"lotSz"`
			MinSz   string `json:"minSz"`
			TickSz  string `json:"tickSz"`
			CtVal   string `json:"ctVal"`
		} `json:"data"`
	}
	if err := json.Unmarshal(raw, &resp); err != nil {
		return nil, errInternal("parse instruments", err)
	}
	out := make(map[string]instrument, len(resp.Data))
	for _, it := range resp.Data {
		if !strings.HasSuffix(it.InstID, "-USDT-SWAP") {
			continue
		}
		out[it.InstID] = instrument{
			LotSize:  parseFloat(it.LotSz),
			MinSize:  parseFloat(it.MinSz),
			TickSize: parseFloat(it.TickSz),
			CtVal:    parseFloat(it.CtVal),
		}
	}
	a.instMu.Lock()
	a.inst = out
	a.instAt = time.Now()
	a.instMu.Unlock()
	return out, nil
}

func parseFloat(s string) float64 {
	f, _ := strconv.ParseFloat(s, 64)
	return f
}

// ── Quantity rounding (in contracts, not coins) ──────────────────────────

func roundContractsToLot(contracts, lot float64) float64 {
	if lot > 0 {
		return math.Floor(contracts/lot) * lot
	}
	return contracts
}

func qtyString(q float64) string {
	s := strconv.FormatFloat(q, 'f', 8, 64)
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
	body, err := a.signedRequest(ctx, creds, http.MethodGet, "/api/v5/account/balance", nil)
	if err != nil {
		return nil, err
	}
	var data []struct {
		Details []struct {
			Ccy     string `json:"ccy"`
			AvailBal string `json:"availBal"`
			CashBal  string `json:"cashBal"`
		} `json:"details"`
	}
	if err := json.Unmarshal(body, &data); err != nil {
		return nil, errInternal("parse balance", err)
	}
	for _, acct := range data {
		for _, d := range acct.Details {
			if d.Ccy != "USDT" {
				continue
			}
			avail := parseFloat(d.AvailBal)
			if avail == 0 {
				avail = parseFloat(d.CashBal)
			}
			return &trade.Balance{TotalUSD: avail, AvailableUSD: avail}, nil
		}
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
	instID := toOKXSymbol(req.Symbol)
	mgn := "isolated"
	if req.MarginMode == trade.MarginCross {
		mgn = "cross"
	}
	// Best-effort: ensure long_short_mode (hedge). Ignore "already set" error.
	_, _ = a.signedRequest(ctx, creds, http.MethodPost,
		"/api/v5/account/set-position-mode",
		map[string]any{"posMode": "long_short_mode"})

	// In hedge + isolated, posSide is required and per-leg. In cross,
	// posSide MUST be omitted. Try long+short legs in isolated mode.
	apply := func(extra map[string]any) error {
		body := map[string]any{
			"instId":  instID,
			"lever":   strconv.Itoa(req.Leverage),
			"mgnMode": mgn,
		}
		for k, v := range extra {
			body[k] = v
		}
		_, err := a.signedRequest(ctx, creds, http.MethodPost,
			"/api/v5/account/set-leverage", body)
		if err == nil {
			return nil
		}
		// 59001 (account level too low for hedge), 59000 (position
		// already exists) — non-fatal, the open will use whatever is
		// already configured.
		if te, ok := err.(*trade.Error); ok && (te.Code == "59001" || te.Code == "59000") {
			return nil
		}
		return err
	}
	if mgn == "isolated" {
		if err := apply(map[string]any{"posSide": "long"}); err != nil {
			return err
		}
		return apply(map[string]any{"posSide": "short"})
	}
	return apply(nil)
}

func (a *Adapter) PlaceOrder(ctx context.Context, creds trade.Creds, req trade.OpenRequest) (*trade.Result, error) {
	if err := req.Validate(); err != nil {
		return nil, err
	}
	instID := toOKXSymbol(req.Symbol)
	insts, err := a.instruments(ctx)
	if err != nil {
		return nil, err
	}
	info, ok := insts[instID]
	if !ok {
		return nil, errUser("symbol %s is not listed on OKX", instID)
	}
	// User asked for `req.Quantity` coins; OKX wants contracts.
	contracts := req.Quantity
	if info.CtVal > 0 {
		contracts = req.Quantity / info.CtVal
	}
	contracts = roundContractsToLot(contracts, info.LotSize)
	if contracts < info.MinSize || contracts <= 0 {
		return nil, errUser("contracts below minSz (%g) for %s", info.MinSize, instID)
	}

	side := "buy"
	posSide := "long"
	if req.Side == trade.SideSell {
		side = "sell"
		posSide = "short"
	}
	tdMode := "isolated"
	if req.MarginMode == trade.MarginCross {
		tdMode = "cross"
	}

	orderParams := map[string]any{
		"instId":  instID,
		"tdMode":  tdMode,
		"side":    side,
		"posSide": posSide,
		"sz":      qtyString(contracts),
	}
	switch req.OrderType {
	case trade.OrderLimit:
		orderParams["ordType"] = "limit"
		orderParams["px"] = strconv.FormatFloat(req.LimitPrice, 'f', -1, 64)
	case trade.OrderStopMarket:
		orderParams["ordType"] = "conditional"
		orderParams["slTriggerPx"] = strconv.FormatFloat(req.StopPrice, 'f', -1, 64)
		orderParams["slOrdPx"] = "-1" // market execution
		orderParams["slTriggerPxType"] = "last"
	case trade.OrderTakeProfitMkt:
		orderParams["ordType"] = "conditional"
		orderParams["tpTriggerPx"] = strconv.FormatFloat(req.StopPrice, 'f', -1, 64)
		orderParams["tpOrdPx"] = "-1" // market execution
		orderParams["tpTriggerPxType"] = "last"
	default:
		orderParams["ordType"] = "market"
	}
	body, err := a.signedRequest(ctx, creds, http.MethodPost, "/api/v5/trade/order", orderParams)
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
	orderID := ""
	clOrdID := ""
	if len(rows) > 0 {
		orderID = rows[0].OrdID
		clOrdID = rows[0].ClOrdID
	}
	// avgPx not returned on the immediate response — Python adapter
	// polls /trade/order to fetch it. We skip that probe here; UI
	// polls /positions for fill confirmation anyway.
	return &trade.Result{
		OrderID:       orderID,
		Symbol:        req.Symbol,
		Side:          req.Side,
		Quantity:      contracts * info.CtVal, // coins, not contracts
		Status:        "NEW",
		ClientOrderID: clOrdID,
		CreatedAt:     time.Now().UTC(),
		Raw:           body,
	}, nil
}

func (a *Adapter) ClosePosition(ctx context.Context, creds trade.Creds, req trade.CloseRequest) (*trade.Result, error) {
	if req.Symbol == "" {
		return nil, errUser("symbol required")
	}
	instID := toOKXSymbol(req.Symbol)
	positions, err := a.ListPositions(ctx, creds, req.Symbol)
	if err != nil {
		return nil, err
	}
	if len(positions) == 0 {
		return &trade.Result{Symbol: req.Symbol, Status: "FLAT"}, nil
	}
	// Match by side when caller specified one (hedge mode can have both).
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
	posSide := "long"
	if p.Side == trade.SideSell {
		posSide = "short"
	}
	mgn := "isolated"
	if p.MarginMode == trade.MarginCross {
		mgn = "cross"
	}
	body, err := a.signedRequest(ctx, creds, http.MethodPost,
		"/api/v5/trade/close-position",
		map[string]any{
			"instId":  instID,
			"mgnMode": mgn,
			"posSide": posSide,
		})
	if err != nil {
		return nil, err
	}
	closeSide := trade.SideSell
	if p.Side == trade.SideSell {
		closeSide = trade.SideBuy
	}
	return &trade.Result{
		Symbol:    req.Symbol,
		Side:      closeSide,
		Quantity:  p.Quantity,
		Status:    "NEW",
		CreatedAt: time.Now().UTC(),
		Raw:       body,
	}, nil
}

func (a *Adapter) ListPositions(ctx context.Context, creds trade.Creds, symbol string) ([]trade.Position, error) {
	path := "/api/v5/account/positions?instType=SWAP"
	if symbol != "" {
		path += "&instId=" + toOKXSymbol(symbol)
	}
	body, err := a.signedRequest(ctx, creds, http.MethodGet, path, nil)
	if err != nil {
		return nil, err
	}
	var rows []struct {
		InstID    string `json:"instId"`
		Pos       string `json:"pos"`        // position size in contracts (signed)
		AvgPx     string `json:"avgPx"`
		MarkPx    string `json:"markPx"`
		Lever     string `json:"lever"`
		Upl       string `json:"upl"`        // unrealized USDT
		PosSide   string `json:"posSide"`    // long / short / net
		MgnMode   string `json:"mgnMode"`    // isolated / cross
	}
	if err := json.Unmarshal(body, &rows); err != nil {
		return nil, errInternal("parse positions", err)
	}
	insts, _ := a.instruments(ctx)
	out := make([]trade.Position, 0, len(rows))
	for _, p := range rows {
		contracts := parseFloat(p.Pos)
		if contracts == 0 {
			continue
		}
		ctVal := 1.0
		if v, ok := insts[p.InstID]; ok && v.CtVal > 0 {
			ctVal = v.CtVal
		}
		side := trade.SideBuy
		if strings.EqualFold(p.PosSide, "short") || (strings.EqualFold(p.PosSide, "net") && contracts < 0) {
			side = trade.SideSell
		}
		mode := trade.MarginIsolated
		if strings.EqualFold(p.MgnMode, "cross") {
			mode = trade.MarginCross
		}
		stripped := strings.TrimSuffix(p.InstID, "-USDT-SWAP")
		out = append(out, trade.Position{
			Symbol:        stripped,
			Side:          side,
			Quantity:      math.Abs(contracts) * ctVal,
			EntryPrice:    parseFloat(p.AvgPx),
			MarkPrice:     parseFloat(p.MarkPx),
			Leverage:      int(parseFloat(p.Lever)),
			UnrealizedPnL: parseFloat(p.Upl),
			Notional:      math.Abs(contracts) * ctVal * parseFloat(p.MarkPx),
			MarginMode:    mode,
		})
	}
	return out, nil
}

// ── Friendly error mapping ───────────────────────────────────────────────

var friendlyMap = map[string]string{
	"50011": "Rate limit exceeded — try again in a moment.",
	"50061": "Too many sub-account requests — slow down.",
	"50100": "API key has no trading permission — enable Trade on your key.",
	"50101": "API key not bound to this passphrase.",
	"50102": "Invalid passphrase.",
	"51000": "Order parameters error (posSide / posMode mismatch).",
	"51008": "Insufficient margin for this order.",
	"51121": "All operations failed — check qty / position state.",
	"51200": "Symbol not listed on OKX.",
	"59000": "Position already exists — leverage cannot be changed mid-position.",
	"59001": "Account level too low for hedge mode.",
}

func friendly(code, msg string) string {
	if v, ok := friendlyMap[code]; ok {
		return v
	}
	if msg != "" {
		return msg
	}
	return "OKX rejected the request."
}

// ── Local errors ─────────────────────────────────────────────────────────

func errUser(msg string, args ...any) *trade.Error {
	return &trade.Error{Kind: trade.KindUser, Message: fmt.Sprintf(msg, args...)}
}

func errInternal(msg string, cause error) *trade.Error {
	return &trade.Error{Kind: trade.KindInternal, Message: msg, Cause: cause}
}

var _ trade.Adapter = (*Adapter)(nil)
