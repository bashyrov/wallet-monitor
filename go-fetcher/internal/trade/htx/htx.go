// HTX (Huobi) USDT-M Linear Swap trade adapter.
//
// Port of `backend/services/trade_adapters/htx.py` (futures-only path —
// the spot side is needed only for fetch_balance and we mirror it here).
//
// Signing flavour: HTX uses a unique multi-line "string-to-sign":
//
//	method "\n" host "\n" path "\n" sortedQuery
//
// where `sortedQuery` excludes the Signature param itself but
// percent-encodes values (RFC 3986). Header-free auth — the API key
// and signature ride in the query string.
//
// Quirks:
//   - Symbol form: contract_code = "BTC-USDT".
//   - Quantity in CONTRACTS (volume) — qty_coins / contract_size.
//   - direction = buy/sell, offset = open/close.
//   - order_price_type = "optimal_20" — HTX's market-equivalent.
//   - Cross-margin endpoint family (/swap_cross_*); isolated has its
//     own family but Python only uses cross.
package htx

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

const (
	futBase = "https://api.hbdm.com"
	futHost = "api.hbdm.com"
)

type Adapter struct {
	httpClient *http.Client

	contractsMu sync.RWMutex
	contracts   map[string]contractInfo
	contractsAt time.Time
}

type contractInfo struct {
	ContractSize float64
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
		contracts: make(map[string]contractInfo, 256),
	}
}

func init() { trade.Register("htx", New()) }

func (a *Adapter) Name() string { return "htx" }

// ── Symbol mapping ───────────────────────────────────────────────────────

func toContractCode(sym string) string { return strings.ToUpper(sym) + "-USDT" }

// ── Signing ──────────────────────────────────────────────────────────────

// htxTimestamp — HTX wants UTC `2006-01-02T15:04:05`.
func htxTimestamp() string {
	return time.Now().UTC().Format("2006-01-02T15:04:05")
}

// canonicalQuery — RFC3986 percent-encoding, sorted alphabetic, excludes "Signature".
func canonicalQuery(params map[string]string) string {
	keys := make([]string, 0, len(params))
	for k := range params {
		if k == "Signature" {
			continue
		}
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		// HTX uses url.QueryEscape (turns space into '+'). Python's adapter
		// uses urllib.parse.quote (which preserves spaces as %20). Server
		// accepts either; we follow Python: url.PathEscape gives RFC3986.
		parts = append(parts, url.QueryEscape(k)+"="+url.QueryEscape(params[k]))
	}
	return strings.Join(parts, "&")
}

func signPayload(method, host, path string, params map[string]string) string {
	return strings.ToUpper(method) + "\n" + host + "\n" + path + "\n" + canonicalQuery(params)
}

func (a *Adapter) signedRequest(
	ctx context.Context, creds trade.Creds, method, path string,
	body any,
) (json.RawMessage, error) {
	params := map[string]string{
		"AccessKeyId":      creds.APIKey,
		"SignatureMethod":  "HmacSHA256",
		"SignatureVersion": "2",
		"Timestamp":        htxTimestamp(),
	}
	pre := signPayload(method, futHost, path, params)
	sig := trade.HMACBase64SHA256(creds.APISecret, pre)
	params["Signature"] = sig

	// Build the URL with EVERY param including signature. Order doesn't
	// matter for the actual request (only for signing).
	keys := make([]string, 0, len(params))
	for k := range params {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, url.QueryEscape(k)+"="+url.QueryEscape(params[k]))
	}
	u := futBase + path + "?" + strings.Join(parts, "&")

	var bodyReader io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return nil, errInternal("marshal body", err)
		}
		bodyReader = strings.NewReader(string(b))
	}
	req, err := http.NewRequestWithContext(ctx, method, u, bodyReader)
	if err != nil {
		return nil, err
	}
	if method != http.MethodGet {
		req.Header.Set("Content-Type", "application/json")
	}
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
		Status string          `json:"status"`
		ErrMsg string          `json:"err_msg"`
		ErrCd  json.Number     `json:"err_code"`
		Data   json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(raw, &env); err != nil {
		return nil, errInternal("parse envelope", err)
	}
	if env.Status == "error" {
		code := string(env.ErrCd)
		return nil, &trade.Error{Kind: trade.KindExchange, Code: code, Message: friendly(code, env.ErrMsg)}
	}
	return env.Data, nil
}

func parseError(status int, body []byte) *trade.Error {
	var env struct {
		Status string      `json:"status"`
		ErrMsg string      `json:"err_msg"`
		ErrCd  json.Number `json:"err_code"`
	}
	_ = json.Unmarshal(body, &env)
	code := string(env.ErrCd)
	if status == 429 {
		return &trade.Error{Kind: trade.KindRateLimit, Code: code, Message: friendly(code, env.ErrMsg)}
	}
	msg := env.ErrMsg
	if msg == "" {
		msg = strings.TrimSpace(string(body))
	}
	return &trade.Error{Kind: trade.KindExchange, Code: code, Message: friendly(code, msg)}
}

// ── Contracts cache ─────────────────────────────────────────────────────

func (a *Adapter) loadContracts(ctx context.Context) (map[string]contractInfo, error) {
	a.contractsMu.RLock()
	cached := a.contracts
	at := a.contractsAt
	a.contractsMu.RUnlock()
	if cached != nil && time.Since(at) < contractsTTL {
		return cached, nil
	}
	u := futBase + "/linear-swap-api/v1/swap_contract_info?support_margin_mode=cross"
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
		Status string `json:"status"`
		Data   []struct {
			ContractCode string      `json:"contract_code"`
			ContractSize json.Number `json:"contract_size"`
		} `json:"data"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil || doc.Status == "error" {
		if cached != nil {
			return cached, nil
		}
		return nil, errInternal("parse contracts", err)
	}
	out := make(map[string]contractInfo, len(doc.Data))
	for _, c := range doc.Data {
		cs, _ := c.ContractSize.Float64()
		out[c.ContractCode] = contractInfo{ContractSize: cs}
	}
	a.contractsMu.Lock()
	a.contracts = out
	a.contractsAt = time.Now()
	a.contractsMu.Unlock()
	return out, nil
}

// ── Adapter methods ──────────────────────────────────────────────────────

func (a *Adapter) GetBalance(ctx context.Context, creds trade.Creds) (*trade.Balance, error) {
	body, err := a.signedRequest(ctx, creds, http.MethodPost,
		"/linear-swap-api/v1/swap_cross_account_info",
		map[string]any{})
	if err != nil {
		return nil, err
	}
	var rows []struct {
		MarginAsset      string      `json:"margin_asset"`
		MarginBalance    json.Number `json:"margin_balance"`
		MarginAvailable  json.Number `json:"margin_available"`
	}
	if err := json.Unmarshal(body, &rows); err != nil {
		return nil, errInternal("parse account-info", err)
	}
	for _, r := range rows {
		if r.MarginAsset != "USDT" {
			continue
		}
		avail, _ := r.MarginAvailable.Float64()
		total, _ := r.MarginBalance.Float64()
		if total == 0 {
			total = avail
		}
		return &trade.Balance{TotalUSD: total, AvailableUSD: avail}, nil
	}
	return &trade.Balance{}, nil
}

func (a *Adapter) SetLeverage(ctx context.Context, creds trade.Creds, req trade.LeverageRequest) error {
	if req.Leverage <= 0 {
		return errUser("leverage must be > 0")
	}
	_, err := a.signedRequest(ctx, creds, http.MethodPost,
		"/linear-swap-api/v1/swap_cross_switch_lever_rate",
		map[string]any{
			"contract_code": toContractCode(req.Symbol),
			"lever_rate":    req.Leverage,
		})
	if err != nil {
		te, ok := err.(*trade.Error)
		if ok && (strings.Contains(strings.ToLower(te.Message), "no need") ||
			strings.Contains(strings.ToLower(te.Message), "same")) {
			return nil
		}
	}
	return err
}

func (a *Adapter) PlaceOrder(ctx context.Context, creds trade.Creds, req trade.OpenRequest) (*trade.Result, error) {
	if err := req.Validate(); err != nil {
		return nil, err
	}
	cc := toContractCode(req.Symbol)
	contracts, err := a.loadContracts(ctx)
	if err != nil {
		return nil, err
	}
	info, ok := contracts[cc]
	if !ok || info.ContractSize <= 0 {
		return nil, errUser("contract %s not active on HTX", cc)
	}
	volume := int64(math.Round(req.Quantity / info.ContractSize))
	if volume <= 0 {
		return nil, errUser("qty %g below 1 contract (%g %s)",
			req.Quantity, info.ContractSize, req.Symbol)
	}
	dir := "buy"
	if req.Side == trade.SideSell {
		dir = "sell"
	}
	body, err := a.signedRequest(ctx, creds, http.MethodPost,
		"/linear-swap-api/v1/swap_cross_order",
		map[string]any{
			"contract_code":    cc,
			"volume":           volume,
			"direction":        dir,
			"offset":           "open",
			"lever_rate":       req.Leverage,
			"order_price_type": "optimal_20",
		})
	if err != nil {
		return nil, err
	}
	var resp struct {
		OrderIDStr string `json:"order_id_str"`
		OrderID    json.Number `json:"order_id"`
	}
	_ = json.Unmarshal(body, &resp)
	orderID := resp.OrderIDStr
	if orderID == "" {
		orderID = string(resp.OrderID)
	}
	return &trade.Result{
		OrderID:   orderID,
		Symbol:    req.Symbol,
		Side:      req.Side,
		Quantity:  float64(volume) * info.ContractSize,
		Status:    "NEW",
		CreatedAt: time.Now().UTC(),
		Raw:       body,
	}, nil
}

func (a *Adapter) ClosePosition(ctx context.Context, creds trade.Creds, req trade.CloseRequest) (*trade.Result, error) {
	if req.Symbol == "" {
		return nil, errUser("symbol required")
	}
	cc := toContractCode(req.Symbol)
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
	contracts, err := a.loadContracts(ctx)
	if err != nil {
		return nil, err
	}
	info := contracts[cc]
	if info.ContractSize <= 0 {
		return nil, errUser("contract %s not active on HTX", cc)
	}
	volume := int64(math.Round(p.Quantity / info.ContractSize))
	if volume <= 0 {
		return &trade.Result{Symbol: req.Symbol, Status: "FLAT"}, nil
	}
	dir := "sell"
	if p.Side == trade.SideSell {
		dir = "buy"
	}
	body, err := a.signedRequest(ctx, creds, http.MethodPost,
		"/linear-swap-api/v1/swap_cross_order",
		map[string]any{
			"contract_code":    cc,
			"volume":           volume,
			"direction":        dir,
			"offset":           "close",
			"lever_rate":       p.Leverage,
			"order_price_type": "optimal_20",
		})
	if err != nil {
		return nil, err
	}
	var resp struct {
		OrderIDStr string      `json:"order_id_str"`
		OrderID    json.Number `json:"order_id"`
	}
	_ = json.Unmarshal(body, &resp)
	orderID := resp.OrderIDStr
	if orderID == "" {
		orderID = string(resp.OrderID)
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
	body, err := a.signedRequest(ctx, creds, http.MethodPost,
		"/linear-swap-api/v1/swap_cross_position_info",
		map[string]any{})
	if err != nil {
		return nil, err
	}
	var rows []struct {
		ContractCode string      `json:"contract_code"`
		Direction    string      `json:"direction"` // buy / sell
		Volume       json.Number `json:"volume"`
		CostOpen     json.Number `json:"cost_open"`
		LastPrice    json.Number `json:"last_price"`
		LeverRate    json.Number `json:"lever_rate"`
		Profit       json.Number `json:"profit_unreal"`
	}
	if err := json.Unmarshal(body, &rows); err != nil {
		return nil, errInternal("parse positions", err)
	}
	contracts, _ := a.loadContracts(ctx)
	wantSym := strings.ToUpper(symbol)
	out := make([]trade.Position, 0, len(rows))
	for _, p := range rows {
		base := strings.TrimSuffix(p.ContractCode, "-USDT")
		if wantSym != "" && base != wantSym {
			continue
		}
		vol, _ := p.Volume.Float64()
		if vol == 0 {
			continue
		}
		side := trade.SideBuy
		if strings.EqualFold(p.Direction, "sell") {
			side = trade.SideSell
		}
		entry, _ := p.CostOpen.Float64()
		mark, _ := p.LastPrice.Float64()
		lev, _ := p.LeverRate.Float64()
		upl, _ := p.Profit.Float64()
		cs := 1.0
		if c, ok := contracts[p.ContractCode]; ok && c.ContractSize > 0 {
			cs = c.ContractSize
		}
		coins := vol * cs
		out = append(out, trade.Position{
			Symbol:        base,
			Side:          side,
			Quantity:      coins,
			EntryPrice:    entry,
			MarkPrice:     mark,
			Leverage:      int(lev),
			UnrealizedPnL: upl,
			Notional:      coins * mark,
			MarginMode:    trade.MarginCross,
		})
	}
	return out, nil
}

// ── Friendly map ────────────────────────────────────────────────────────

var friendlyMap = map[string]string{
	"403":   "Forbidden — check IP whitelist + key permissions.",
	"1003":  "Invalid signature.",
	"1006":  "Invalid API key.",
	"1023":  "Insufficient margin balance.",
	"1066":  "Order qty below contract minimum.",
	"1071":  "Position size exceeds account limit.",
}

func friendly(code, msg string) string {
	if v, ok := friendlyMap[code]; ok {
		return v
	}
	if msg != "" {
		return msg
	}
	return "HTX rejected the request."
}

func errUser(msg string, args ...any) *trade.Error {
	return &trade.Error{Kind: trade.KindUser, Message: fmt.Sprintf(msg, args...)}
}

func errInternal(msg string, cause error) *trade.Error {
	return &trade.Error{Kind: trade.KindInternal, Message: msg, Cause: cause}
}

func parseFloat(s string) float64 {
	f, _ := strconv.ParseFloat(s, 64)
	return f
}

var _ trade.Adapter = (*Adapter)(nil)
var _ = parseFloat // for future use
