// Bybit V5 USDT-Perpetual trade adapter.
//
// Port of `backend/services/trade_adapters/bybit.py`.
//
// Signing:
//
//	signature = HMAC_SHA256(secret, ts || apiKey || recvWindow || (queryString | jsonBody))
//
// Headers (every signed request):
//
//	X-BAPI-API-KEY:       <key>
//	X-BAPI-SIGN:          <hex digest>
//	X-BAPI-SIGN-TYPE:     2
//	X-BAPI-TIMESTAMP:     <ms>
//	X-BAPI-RECV-WINDOW:   5000
//
// Caches: instruments-info per symbol (10 min TTL).
package bybit

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

	"golang.org/x/sync/errgroup"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

const baseURL = "https://api.bybit.com"

type Adapter struct {
	httpClient *http.Client

	infoMu sync.RWMutex
	info   map[string]symbolInfo
}

type symbolInfo struct {
	QtyStep     float64
	MinOrderQty float64
	MinNotional float64
	TickSize    float64
	Status      string
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
		info: make(map[string]symbolInfo, 256),
	}
}

func init() { trade.Register("bybit", New()) }

func (a *Adapter) Name() string { return "bybit" }

// ── Symbol mapping ───────────────────────────────────────────────────────

func toBybit(sym string) string { return strings.ToUpper(sym) + "USDT" }

// ── Signing ──────────────────────────────────────────────────────────────

func (a *Adapter) signedRequest(
	ctx context.Context, creds trade.Creds, method, path string,
	params map[string]string, body any,
) (json.RawMessage, error) {
	ts := strconv.FormatInt(time.Now().UnixMilli(), 10)
	const recv = "5000"

	var (
		bodyBytes []byte
		queryStr  string
		signSrc   string
		err       error
	)
	if method == http.MethodGet {
		queryStr = trade.SortedFormQuery(params)
		signSrc = ts + creds.APIKey + recv + queryStr
	} else {
		// POST: body is the JSON we send AND the signed payload.
		if body != nil {
			bodyBytes, err = json.Marshal(body)
			if err != nil {
				return nil, errInternal("marshal body", err)
			}
		} else {
			bodyBytes = []byte("{}")
		}
		signSrc = ts + creds.APIKey + recv + string(bodyBytes)
	}
	sig := trade.HMACHexSHA256(creds.APISecret, signSrc)

	url := baseURL + path
	if method == http.MethodGet && queryStr != "" {
		url += "?" + queryStr
	}
	req, err := http.NewRequestWithContext(ctx, method, url,
		strings.NewReader(string(bodyBytes)))
	if err != nil {
		return nil, err
	}
	if method != http.MethodGet {
		req.Header.Set("Content-Type", "application/json")
	}
	req.Header.Set("X-BAPI-API-KEY", creds.APIKey)
	req.Header.Set("X-BAPI-SIGN", sig)
	req.Header.Set("X-BAPI-SIGN-TYPE", "2")
	req.Header.Set("X-BAPI-TIMESTAMP", ts)
	req.Header.Set("X-BAPI-RECV-WINDOW", recv)

	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, &trade.Error{Kind: trade.KindTransient, Message: err.Error(), Cause: err}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)

	if resp.StatusCode >= 400 {
		return nil, parseError(resp.StatusCode, raw)
	}
	// Bybit V5 always returns 200 with retCode != 0 on logical errors.
	var env struct {
		RetCode int             `json:"retCode"`
		RetMsg  string          `json:"retMsg"`
		Result  json.RawMessage `json:"result"`
	}
	if err := json.Unmarshal(raw, &env); err != nil {
		return nil, errInternal("parse envelope", err)
	}
	if env.RetCode != 0 {
		return nil, parseError(resp.StatusCode, raw)
	}
	return env.Result, nil
}

func parseError(status int, body []byte) *trade.Error {
	var env struct {
		RetCode int    `json:"retCode"`
		RetMsg  string `json:"retMsg"`
	}
	_ = json.Unmarshal(body, &env)
	codeStr := ""
	if env.RetCode != 0 {
		codeStr = strconv.Itoa(env.RetCode)
	}
	msg := friendly(codeStr, env.RetMsg)
	if env.RetCode == 10006 || status == 429 {
		return &trade.Error{Kind: trade.KindRateLimit, Code: codeStr, Message: msg}
	}
	return &trade.Error{Kind: trade.KindExchange, Code: codeStr, Message: msg}
}

// ── Caches ───────────────────────────────────────────────────────────────

func (a *Adapter) instrumentInfo(ctx context.Context, sym string) (symbolInfo, error) {
	a.infoMu.RLock()
	hit, ok := a.info[sym]
	a.infoMu.RUnlock()
	if ok && time.Since(hit.At) < infoTTL {
		return hit, nil
	}
	url := baseURL + "/v5/market/instruments-info?category=linear&symbol=" + sym
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	resp, err := a.httpClient.Do(req)
	if err != nil {
		if ok {
			return hit, nil // stale-cache fallback
		}
		return symbolInfo{}, &trade.Error{Kind: trade.KindTransient, Message: err.Error()}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var env struct {
		Result struct {
			List []struct {
				Symbol         string `json:"symbol"`
				Status         string `json:"status"`
				LotSizeFilter  struct {
					QtyStep          string `json:"qtyStep"`
					MinOrderQty      string `json:"minOrderQty"`
					MinNotionalValue string `json:"minNotionalValue"`
				} `json:"lotSizeFilter"`
				PriceFilter struct {
					TickSize string `json:"tickSize"`
				} `json:"priceFilter"`
			} `json:"list"`
		} `json:"result"`
	}
	if err := json.Unmarshal(raw, &env); err != nil {
		return symbolInfo{}, errInternal("parse instruments-info", err)
	}
	if len(env.Result.List) == 0 {
		return symbolInfo{}, &trade.Error{
			Kind:    trade.KindUser,
			Message: fmt.Sprintf("symbol %s not listed on Bybit", sym),
		}
	}
	it := env.Result.List[0]
	out := symbolInfo{
		QtyStep:     parseFloat(it.LotSizeFilter.QtyStep),
		MinOrderQty: parseFloat(it.LotSizeFilter.MinOrderQty),
		MinNotional: parseFloat(it.LotSizeFilter.MinNotionalValue),
		TickSize:    parseFloat(it.PriceFilter.TickSize),
		Status:      it.Status,
		At:          time.Now(),
	}
	a.infoMu.Lock()
	a.info[sym] = out
	a.infoMu.Unlock()
	return out, nil
}

func parseFloat(s string) float64 {
	f, _ := strconv.ParseFloat(s, 64)
	return f
}

// ── Quantity rounding ────────────────────────────────────────────────────

func roundToStep(qty, step, minQty float64) float64 {
	if step > 0 {
		qty = math.Floor(qty/step) * step
	}
	if minQty > 0 && qty < minQty {
		return 0
	}
	return qty
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
	body, err := a.signedRequest(ctx, creds, http.MethodGet,
		"/v5/account/wallet-balance", map[string]string{"accountType": "UNIFIED"}, nil)
	if err != nil {
		return nil, err
	}
	var env struct {
		List []struct {
			Coin []struct {
				Coin                  string `json:"coin"`
				WalletBalance         string `json:"walletBalance"`
				AvailableToWithdraw   string `json:"availableToWithdraw"`
			} `json:"coin"`
		} `json:"list"`
	}
	if err := json.Unmarshal(body, &env); err != nil {
		return nil, errInternal("parse balance", err)
	}
	for _, row := range env.List {
		for _, c := range row.Coin {
			if c.Coin != "USDT" {
				continue
			}
			avail := parseFloat(c.AvailableToWithdraw)
			if avail == 0 {
				avail = parseFloat(c.WalletBalance)
			}
			total := parseFloat(c.WalletBalance)
			return &trade.Balance{TotalUSD: total, AvailableUSD: avail}, nil
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
	sym := toBybit(req.Symbol)
	tradeMode := 1 // isolated
	if req.MarginMode == trade.MarginCross {
		tradeMode = 0
	}

	g, gctx := errgroup.WithContext(ctx)

	g.Go(func() error {
		// Per-symbol margin-mode switch (Classic + UTA-Inverse path).
		_, err := a.signedRequest(gctx, creds, http.MethodPost,
			"/v5/position/switch-isolated", nil, map[string]any{
				"category":     "linear",
				"symbol":       sym,
				"tradeMode":    tradeMode,
				"buyLeverage":  strconv.Itoa(req.Leverage),
				"sellLeverage": strconv.Itoa(req.Leverage),
			})
		if err == nil {
			return nil
		}
		te, ok := err.(*trade.Error)
		if !ok {
			return err
		}
		// Already-set / not-modified codes: 110026, 110043, 110027 — non-fatal.
		if te.Code == "110026" || te.Code == "110043" || te.Code == "110027" {
			return nil
		}
		// 100028: UTA-Linear forbids the per-symbol path; switch
		// account-wide instead.
		if te.Code == "100028" {
			setting := "ISOLATED_MARGIN"
			if req.MarginMode == trade.MarginCross {
				setting = "REGULAR_MARGIN"
			}
			_, err := a.signedRequest(gctx, creds, http.MethodPost,
				"/v5/account/set-margin-mode", nil,
				map[string]any{"setMarginMode": setting})
			if err == nil {
				return nil
			}
			te2, ok := err.(*trade.Error)
			if ok && (te2.Code == "30086" ||
				strings.Contains(strings.ToLower(te2.Message), "already")) {
				return nil
			}
			log.L().Warn().Err(err).Str("sym", sym).Msg("bybit set-margin-mode")
			return nil // non-fatal — order can still proceed
		}
		return err
	})

	g.Go(func() error {
		_, err := a.signedRequest(gctx, creds, http.MethodPost,
			"/v5/position/set-leverage", nil, map[string]any{
				"category":     "linear",
				"symbol":       sym,
				"buyLeverage":  strconv.Itoa(req.Leverage),
				"sellLeverage": strconv.Itoa(req.Leverage),
			})
		if err == nil {
			return nil
		}
		// 110043 — leverage not modified (already set).
		if te, ok := err.(*trade.Error); ok && te.Code == "110043" {
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
	sym := toBybit(req.Symbol)
	info, err := a.instrumentInfo(ctx, sym)
	if err != nil {
		return nil, err
	}
	if info.Status != "" && !strings.EqualFold(info.Status, "Trading") {
		return nil, errUser("symbol %s is not trading on Bybit (status=%s)", sym, info.Status)
	}
	qty := roundToStep(req.Quantity, info.QtyStep, info.MinOrderQty)
	if qty <= 0 {
		return nil, errUser("quantity below minimum (%g %s)", info.MinOrderQty, req.Symbol)
	}
	side := "Buy"
	if req.Side == trade.SideSell {
		side = "Sell"
	}
	orderBody := map[string]any{
		"category": "linear",
		"symbol":   sym,
		"side":     side,
		"qty":      qtyString(qty),
	}
	switch req.OrderType {
	case trade.OrderLimit:
		orderBody["orderType"] = "Limit"
		orderBody["price"] = strconv.FormatFloat(req.LimitPrice, 'f', -1, 64)
		orderBody["timeInForce"] = "GTC"
	case trade.OrderStopMarket:
		orderBody["orderType"] = "Market"
		orderBody["triggerPrice"] = strconv.FormatFloat(req.StopPrice, 'f', -1, 64)
		orderBody["triggerDirection"] = 2 // price falls to stop
		orderBody["tpslMode"] = "Full"
	case trade.OrderTakeProfitMkt:
		orderBody["orderType"] = "Market"
		orderBody["triggerPrice"] = strconv.FormatFloat(req.StopPrice, 'f', -1, 64)
		orderBody["triggerDirection"] = 1 // price rises to TP
		orderBody["tpslMode"] = "Full"
	default:
		orderBody["orderType"] = "Market"
	}
	body, err := a.signedRequest(ctx, creds, http.MethodPost, "/v5/order/create", nil, orderBody)
	if err != nil {
		return nil, err
	}
	var resp struct {
		OrderID  string `json:"orderId"`
		ClientID string `json:"orderLinkId"`
	}
	_ = json.Unmarshal(body, &resp)
	res := &trade.Result{
		OrderID:       resp.OrderID,
		Symbol:        req.Symbol,
		Side:          req.Side,
		Quantity:      qty,
		Status:        "NEW",
		ClientOrderID: resp.ClientID,
		CreatedAt:     time.Now().UTC(),
		Raw:           body,
	}
	// Fetch fill price — Bybit doesn't return avgPrice on placement; poll once.
	if resp.OrderID != "" {
		if avg := a.fetchBybitAvgPrice(ctx, creds, sym, resp.OrderID); avg > 0 {
			res.AvgPrice = avg
		}
	}
	return res, nil
}

// fetchBybitAvgPrice polls /v5/order/history once (after 300ms) to get the
// actual average fill price of a freshly-placed market order.
func (a *Adapter) fetchBybitAvgPrice(ctx context.Context, creds trade.Creds, sym, orderID string) float64 {
	timer := time.NewTimer(300 * time.Millisecond)
	defer timer.Stop()
	select {
	case <-timer.C:
	case <-ctx.Done():
		return 0
	}
	data, err := a.signedRequest(ctx, creds, http.MethodGet, "/v5/order/history", map[string]string{
		"category": "linear",
		"symbol":   sym,
		"orderId":  orderID,
	}, nil)
	if err != nil {
		return 0
	}
	var env struct {
		List []struct {
			AvgPrice string `json:"avgPrice"`
		} `json:"list"`
	}
	_ = json.Unmarshal(data, &env)
	if len(env.List) > 0 {
		v, _ := strconv.ParseFloat(env.List[0].AvgPrice, 64)
		return v
	}
	return 0
}

func (a *Adapter) ClosePosition(ctx context.Context, creds trade.Creds, req trade.CloseRequest) (*trade.Result, error) {
	if req.Symbol == "" {
		return nil, errUser("symbol required")
	}
	positions, err := a.ListPositions(ctx, creds, req.Symbol)
	if err != nil {
		return nil, err
	}
	if len(positions) == 0 {
		return &trade.Result{Symbol: req.Symbol, Status: "FLAT"}, nil
	}
	p := positions[0]
	reduceSide := "Sell"
	if p.Side == trade.SideSell {
		reduceSide = "Buy"
	}
	body, err := a.signedRequest(ctx, creds, http.MethodPost,
		"/v5/order/create", nil, map[string]any{
			"category":   "linear",
			"symbol":     toBybit(req.Symbol),
			"side":       reduceSide,
			"orderType":  "Market",
			"qty":        qtyString(p.Quantity),
			"reduceOnly": true,
		})
	if err != nil {
		return nil, err
	}
	var resp struct {
		OrderID string `json:"orderId"`
	}
	_ = json.Unmarshal(body, &resp)
	closeSide := trade.SideSell
	if reduceSide == "Buy" {
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
	params := map[string]string{"category": "linear"}
	if symbol != "" {
		params["symbol"] = toBybit(symbol)
	} else {
		params["settleCoin"] = "USDT"
	}
	body, err := a.signedRequest(ctx, creds, http.MethodGet,
		"/v5/position/list", params, nil)
	if err != nil {
		return nil, err
	}
	var env struct {
		List []struct {
			Symbol        string `json:"symbol"`
			Side          string `json:"side"`
			Size          string `json:"size"`
			AvgPrice      string `json:"avgPrice"`
			MarkPrice     string `json:"markPrice"`
			Leverage      string `json:"leverage"`
			UnrealisedPnL string `json:"unrealisedPnl"`
			TradeMode     int    `json:"tradeMode"` // 0=cross/UTA, 1=isolated
		} `json:"list"`
	}
	if err := json.Unmarshal(body, &env); err != nil {
		return nil, errInternal("parse positions", err)
	}
	out := make([]trade.Position, 0, len(env.List))
	for _, p := range env.List {
		qty := parseFloat(p.Size)
		if qty == 0 {
			continue
		}
		side := trade.SideBuy
		if p.Side == "Sell" {
			side = trade.SideSell
		}
		mode := trade.MarginCross
		if p.TradeMode == 1 {
			mode = trade.MarginIsolated
		}
		stripped := strings.TrimSuffix(p.Symbol, "USDT")
		out = append(out, trade.Position{
			Symbol:        stripped,
			Side:          side,
			Quantity:      qty,
			EntryPrice:    parseFloat(p.AvgPrice),
			MarkPrice:     parseFloat(p.MarkPrice),
			Leverage:      int(parseFloat(p.Leverage)),
			UnrealizedPnL: parseFloat(p.UnrealisedPnL),
			Notional:      qty * parseFloat(p.MarkPrice),
			MarginMode:    mode,
		})
	}
	return out, nil
}

// ── Friendly error mapping ───────────────────────────────────────────────

var friendlyMap = map[string]string{
	"10001":  "Bad request to Bybit.",
	"10002":  "Request timeout or bad signature.",
	"10003":  "Invalid API key.",
	"10004":  "Invalid signature.",
	"10005":  "API key permissions insufficient.",
	"10006":  "Rate limit exceeded — try again in a moment.",
	"10010":  "IP not allowed — add the server IP to your key's whitelist.",
	"110004": "Insufficient balance for margin.",
	"110007": "Insufficient available balance.",
	"110012": "Order quantity exceeds position limit.",
	"110017": "Order qty below minimum.",
	"110020": "Order qty not a multiple of lot step.",
	"110025": "Position side not matched (hedge mode).",
	"110043": "Leverage not modified.",
	"110093": "Symbol is not trading right now.",
}

func friendly(code, msg string) string {
	if v, ok := friendlyMap[code]; ok {
		return v
	}
	if msg != "" {
		return msg
	}
	return "Bybit rejected the request."
}

// ── Local errors ─────────────────────────────────────────────────────────

func errUser(msg string, args ...any) *trade.Error {
	return &trade.Error{Kind: trade.KindUser, Message: fmt.Sprintf(msg, args...)}
}

func errInternal(msg string, cause error) *trade.Error {
	return &trade.Error{Kind: trade.KindInternal, Message: msg, Cause: cause}
}

var _ trade.Adapter = (*Adapter)(nil)
