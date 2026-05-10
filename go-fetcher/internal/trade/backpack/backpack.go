// Backpack Exchange trade adapter (spot — perpetuals coming).
//
// Port of `backend/services/trade_adapters/backpack.py`.
//
// Signing: Ed25519 over the canonical sign-string
//
//	instruction=<X>&<sortedParams>&timestamp=<ms>&window=<60000>
//
// where the body params (or query params for GETs) are folded in
// alphabetic order. Output is base64.
//
// Headers:
//
//	X-API-Key:    <api_key>            (base64 ed25519 public key)
//	X-Signature:  <base64 sig>
//	X-Timestamp:  <ms>
//	X-Window:     60000
//
// API key encoding: api_key is base64 ed25519 PUBLIC key. Secret is
// base64 ed25519 SEED (32 bytes). We derive the private key from the
// seed via ed25519.NewKeyFromSeed.
//
// Quirks:
//   - Symbol form: "BTC_USDT".
//   - Side encoded as "Bid" (buy) / "Ask" (sell).
//   - SetLeverage is a no-op (Backpack spot).
//   - "Positions" are non-zero base-asset balances (pseudo positions).
package backpack

import (
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

const (
	baseURL    = "https://api.backpack.exchange"
	recvWindow = "60000"
)

type Adapter struct {
	httpClient *http.Client
}

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
	}
}

func init() { trade.Register("backpack", New()) }

func (a *Adapter) Name() string { return "backpack" }

func toBPSymbol(sym string) string { return strings.ToUpper(sym) + "_USDT" }

// ── Signing ──────────────────────────────────────────────────────────────

func buildSignString(instruction string, ts int64, params map[string]string) string {
	parts := []string{"instruction=" + instruction}
	keys := make([]string, 0, len(params))
	for k := range params {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		parts = append(parts, k+"="+params[k])
	}
	parts = append(parts, "timestamp="+strconv.FormatInt(ts, 10))
	parts = append(parts, "window="+recvWindow)
	return strings.Join(parts, "&")
}

func signEd25519(message, seedB64 string) (string, error) {
	seed, err := base64.StdEncoding.DecodeString(seedB64)
	if err != nil {
		return "", err
	}
	if len(seed) != ed25519.SeedSize {
		return "", fmt.Errorf("invalid seed length %d (want %d)", len(seed), ed25519.SeedSize)
	}
	priv := ed25519.NewKeyFromSeed(seed)
	sig := ed25519.Sign(priv, []byte(message))
	return base64.StdEncoding.EncodeToString(sig), nil
}

func (a *Adapter) signedRequest(
	ctx context.Context, creds trade.Creds, method, path, instruction string,
	params map[string]string, body map[string]string,
) (json.RawMessage, error) {
	ts := time.Now().UnixMilli()
	signParams := params
	if signParams == nil {
		signParams = body
	}
	signStr := buildSignString(instruction, ts, signParams)
	sig, err := signEd25519(signStr, creds.APISecret)
	if err != nil {
		return nil, errInternal("ed25519 sign", err)
	}

	u := baseURL + path
	if method == http.MethodGet && len(params) > 0 {
		u += "?" + trade.SortedFormQuery(params)
	}

	var bodyReader io.Reader
	if method != http.MethodGet && body != nil {
		// Body is JSON for POST/DELETE — Python uses json= not form.
		// Convert string-string map to {string: any} so JSON keeps types.
		obj := map[string]any{}
		for k, v := range body {
			obj[k] = v
		}
		b, err := json.Marshal(obj)
		if err != nil {
			return nil, errInternal("marshal body", err)
		}
		bodyReader = strings.NewReader(string(b))
	}
	req, err := http.NewRequestWithContext(ctx, method, u, bodyReader)
	if err != nil {
		return nil, err
	}
	req.Header.Set("X-API-Key", creds.APIKey)
	req.Header.Set("X-Signature", sig)
	req.Header.Set("X-Timestamp", strconv.FormatInt(ts, 10))
	req.Header.Set("X-Window", recvWindow)
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
	if len(raw) == 0 {
		return json.RawMessage("{}"), nil
	}
	return raw, nil
}

func parseError(status int, body []byte) *trade.Error {
	var env struct {
		Message string `json:"message"`
		Error   string `json:"error"`
	}
	_ = json.Unmarshal(body, &env)
	msg := env.Message
	if msg == "" {
		msg = env.Error
	}
	if msg == "" {
		msg = strings.TrimSpace(string(body))
	}
	if status == 429 {
		return &trade.Error{Kind: trade.KindRateLimit, Message: msg}
	}
	return &trade.Error{Kind: trade.KindExchange, Message: msg}
}

// ── Adapter methods ──────────────────────────────────────────────────────

func (a *Adapter) GetBalance(ctx context.Context, creds trade.Creds) (*trade.Balance, error) {
	body, err := a.signedRequest(ctx, creds, http.MethodGet,
		"/api/v1/capital", "balanceQuery", nil, nil)
	if err != nil {
		return nil, err
	}
	var data map[string]struct {
		Available string `json:"available"`
		Locked    string `json:"locked"`
	}
	if err := json.Unmarshal(body, &data); err != nil {
		return nil, errInternal("parse balance", err)
	}
	if u, ok := data["USDT"]; ok {
		avail, _ := strconv.ParseFloat(u.Available, 64)
		locked, _ := strconv.ParseFloat(u.Locked, 64)
		total := avail + locked
		return &trade.Balance{TotalUSD: total, AvailableUSD: avail}, nil
	}
	return &trade.Balance{}, nil
}

func (a *Adapter) SetLeverage(_ context.Context, _ trade.Creds, _ trade.LeverageRequest) error {
	return nil // Backpack spot has no leverage API
}

func (a *Adapter) PlaceOrder(ctx context.Context, creds trade.Creds, req trade.OpenRequest) (*trade.Result, error) {
	if err := req.Validate(); err != nil {
		return nil, err
	}
	side := "Bid"
	if req.Side == trade.SideSell {
		side = "Ask"
	}
	orderParams := map[string]string{
		"symbol":   toBPSymbol(req.Symbol),
		"side":     side,
		"quantity": qtyString(req.Quantity),
	}
	switch req.OrderType {
	case trade.OrderLimit:
		orderParams["orderType"] = "Limit"
		orderParams["price"] = strconv.FormatFloat(req.LimitPrice, 'f', -1, 64)
		orderParams["timeInForce"] = "GTC"
	case trade.OrderStopMarket:
		orderParams["orderType"] = "StopMarket"
		orderParams["triggerPrice"] = strconv.FormatFloat(req.StopPrice, 'f', -1, 64)
	case trade.OrderTakeProfitMkt:
		orderParams["orderType"] = "TakeProfitMarket"
		orderParams["triggerPrice"] = strconv.FormatFloat(req.StopPrice, 'f', -1, 64)
	default:
		orderParams["orderType"] = "Market"
	}
	body, err := a.signedRequest(ctx, creds, http.MethodPost,
		"/api/v1/order", "orderExecute", nil, orderParams)
	if err != nil {
		return nil, err
	}
	var resp struct {
		ID       string `json:"id"`
		OrderID  string `json:"orderId"`
		Price    string `json:"price"`
		AvgPrice string `json:"avgPrice"`
	}
	_ = json.Unmarshal(body, &resp)
	id := resp.ID
	if id == "" {
		id = resp.OrderID
	}
	avgStr := resp.AvgPrice
	if avgStr == "" {
		avgStr = resp.Price
	}
	avg, _ := strconv.ParseFloat(avgStr, 64)
	return &trade.Result{
		OrderID:   id,
		Symbol:    req.Symbol,
		Side:      req.Side,
		Quantity:  req.Quantity,
		AvgPrice:  avg,
		Status:    "NEW",
		CreatedAt: time.Now().UTC(),
		Raw:       body,
	}, nil
}

func (a *Adapter) ClosePosition(ctx context.Context, creds trade.Creds, req trade.CloseRequest) (*trade.Result, error) {
	if req.Symbol == "" {
		return nil, errUser("symbol required")
	}
	// Backpack spot close: sell the full base-asset balance.
	body, err := a.signedRequest(ctx, creds, http.MethodGet,
		"/api/v1/capital", "balanceQuery", nil, nil)
	if err != nil {
		return nil, err
	}
	var data map[string]struct {
		Available string `json:"available"`
	}
	_ = json.Unmarshal(body, &data)
	bal := data[strings.ToUpper(req.Symbol)]
	amt, _ := strconv.ParseFloat(bal.Available, 64)
	if amt <= 0 {
		return &trade.Result{Symbol: req.Symbol, Status: "FLAT"}, nil
	}
	side := "Ask" // sell base asset
	if req.Side == trade.SideSell {
		side = "Bid"
	}
	body, err = a.signedRequest(ctx, creds, http.MethodPost,
		"/api/v1/order", "orderExecute", nil,
		map[string]string{
			"symbol":    toBPSymbol(req.Symbol),
			"side":      side,
			"orderType": "Market",
			"quantity":  qtyString(amt),
		})
	if err != nil {
		return nil, err
	}
	var resp struct {
		ID string `json:"id"`
	}
	_ = json.Unmarshal(body, &resp)
	closeSide := trade.SideSell
	if side == "Bid" {
		closeSide = trade.SideBuy
	}
	return &trade.Result{
		OrderID:   resp.ID,
		Symbol:    req.Symbol,
		Side:      closeSide,
		Quantity:  amt,
		Status:    "NEW",
		CreatedAt: time.Now().UTC(),
		Raw:       body,
	}, nil
}

func (a *Adapter) ListPositions(ctx context.Context, creds trade.Creds, symbol string) ([]trade.Position, error) {
	body, err := a.signedRequest(ctx, creds, http.MethodGet,
		"/api/v1/capital", "balanceQuery", nil, nil)
	if err != nil {
		return nil, err
	}
	var data map[string]struct {
		Available string `json:"available"`
		Locked    string `json:"locked"`
	}
	if err := json.Unmarshal(body, &data); err != nil {
		return nil, errInternal("parse balances", err)
	}
	wantSym := strings.ToUpper(symbol)
	out := make([]trade.Position, 0, len(data))
	for asset, e := range data {
		if asset == "USDT" || asset == "USDC" {
			continue
		}
		avail, _ := strconv.ParseFloat(e.Available, 64)
		locked, _ := strconv.ParseFloat(e.Locked, 64)
		total := avail + locked
		if total <= 0 {
			continue
		}
		if wantSym != "" && strings.ToUpper(asset) != wantSym {
			continue
		}
		out = append(out, trade.Position{
			Symbol:   asset,
			Side:     trade.SideBuy, // spot holdings = "long"
			Quantity: total,
			Leverage: 1,
		})
	}
	return out, nil
}

// ── Helpers ──────────────────────────────────────────────────────────────

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

func errUser(msg string, args ...any) *trade.Error {
	return &trade.Error{Kind: trade.KindUser, Message: fmt.Sprintf(msg, args...)}
}

func errInternal(msg string, cause error) *trade.Error {
	return &trade.Error{Kind: trade.KindInternal, Message: msg, Cause: cause}
}

var _ trade.Adapter = (*Adapter)(nil)
