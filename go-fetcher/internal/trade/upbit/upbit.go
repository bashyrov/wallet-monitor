// Upbit spot trade adapter.
//
// Port of `backend/services/trade_adapters/upbit.py` — spot-only venue
// (no perpetuals). Markets are USDT-quoted ("USDT-BTC").
//
// Auth: JWT HS256 in `Authorization: Bearer <jwt>`. Payload:
//
//	{access_key, nonce (UUID4)}
//	+ {query_hash: SHA512-hex(urlencoded params), query_hash_alg: "SHA512"}
//	  when the request carries params
//
// Params stay ORDERED — the hash preimage and the JSON body are built
// from the same []param slice in the same key order, mirroring Python's
// urlencode(dict) + json.dumps(dict) insertion-order parity.
//
// Upbit market orders are asymmetric: BUY uses ord_type="price" with
// `price` = USDT to spend; SELL uses ord_type="market" with `volume` =
// base quantity. Spot has no positions — ListPositions is always empty
// and ClosePosition market-sells the wallet's base-currency balance.
package upbit

import (
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"crypto/sha512"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

const baseURL = "https://api.upbit.com"

type param struct{ k, v string }

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

func init() { trade.Register("upbit", New()) }

func (a *Adapter) Name() string { return "upbit" }

func toMarket(sym string) string { return "USDT-" + strings.ToUpper(sym) }

// ── Signing ──────────────────────────────────────────────────────────────

func uuid4() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}

func encodeQuery(params []param) string {
	var sb strings.Builder
	for i, p := range params {
		if i > 0 {
			sb.WriteByte('&')
		}
		sb.WriteString(url.QueryEscape(p.k))
		sb.WriteByte('=')
		sb.WriteString(url.QueryEscape(p.v))
	}
	return sb.String()
}

func encodeBody(params []param) []byte {
	var sb strings.Builder
	sb.WriteByte('{')
	for i, p := range params {
		if i > 0 {
			sb.WriteByte(',')
		}
		k, _ := json.Marshal(p.k)
		v, _ := json.Marshal(p.v)
		sb.Write(k)
		sb.WriteByte(':')
		sb.Write(v)
	}
	sb.WriteByte('}')
	return []byte(sb.String())
}

func buildJWT(apiKey, apiSecret string, params []param) string {
	payload := map[string]any{
		"access_key": strings.TrimSpace(apiKey),
		"nonce":      uuid4(),
	}
	if len(params) > 0 {
		sum := sha512.Sum512([]byte(encodeQuery(params)))
		payload["query_hash"] = hex.EncodeToString(sum[:])
		payload["query_hash_alg"] = "SHA512"
	}
	header := base64.RawURLEncoding.EncodeToString([]byte(`{"alg":"HS256","typ":"JWT"}`))
	pj, _ := json.Marshal(payload)
	signing := header + "." + base64.RawURLEncoding.EncodeToString(pj)
	mac := hmac.New(sha256.New, []byte(strings.TrimSpace(apiSecret)))
	mac.Write([]byte(signing))
	return signing + "." + base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

func (a *Adapter) signedRequest(
	ctx context.Context, creds trade.Creds, method, path string, params []param,
) (json.RawMessage, error) {
	var req *http.Request
	var err error
	if method == http.MethodGet {
		u := baseURL + path
		if len(params) > 0 {
			u += "?" + encodeQuery(params)
		}
		req, err = http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	} else {
		req, err = http.NewRequestWithContext(ctx, method, baseURL+path,
			strings.NewReader(string(encodeBody(params))))
		if req != nil {
			req.Header.Set("Content-Type", "application/json")
		}
	}
	if err != nil {
		return nil, errInternal("build request", err)
	}
	req.Header.Set("Authorization", "Bearer "+buildJWT(creds.APIKey, creds.APISecret, params))

	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, &trade.Error{Kind: trade.KindTransient, Message: err.Error(), Cause: err}
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
		Error struct {
			Name    json.RawMessage `json:"name"`
			Message string          `json:"message"`
		} `json:"error"`
	}
	_ = json.Unmarshal(body, &env)
	msg := env.Error.Message
	if msg == "" {
		msg = strings.TrimSpace(string(body))
		if len(msg) > 200 {
			msg = msg[:200]
		}
	}
	if status == 429 {
		return &trade.Error{Kind: trade.KindRateLimit, Message: msg}
	}
	if status == 401 {
		return &trade.Error{Kind: trade.KindUser, Message: "Upbit auth failed: " + msg}
	}
	return &trade.Error{Kind: trade.KindExchange, Message: msg}
}

// ── Public market data ───────────────────────────────────────────────────

func (a *Adapter) lastPrice(ctx context.Context, market string) (float64, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet,
		baseURL+"/v1/ticker?markets="+url.QueryEscape(market), nil)
	if err != nil {
		return 0, errInternal("build ticker request", err)
	}
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return 0, &trade.Error{Kind: trade.KindTransient, Message: err.Error(), Cause: err}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return 0, parseError(resp.StatusCode, raw)
	}
	var rows []struct {
		TradePrice float64 `json:"trade_price"`
	}
	if err := json.Unmarshal(raw, &rows); err != nil || len(rows) == 0 || rows[0].TradePrice <= 0 {
		return 0, &trade.Error{Kind: trade.KindExchange, Message: "Upbit: no price for " + market}
	}
	return rows[0].TradePrice, nil
}

// ── Order shaping ────────────────────────────────────────────────────────

// orderParams — Upbit's asymmetric market-order shape. BUY spends quote
// (`price` = qty × last), SELL sends base `volume`.
func orderParams(symbol string, isBuy bool, quantity, lastPx float64) []param {
	market := toMarket(symbol)
	if isBuy {
		return []param{
			{"market", market},
			{"side", "bid"},
			{"ord_type", "price"},
			{"price", strconv.FormatFloat(quantity*lastPx, 'f', 8, 64)},
		}
	}
	return []param{
		{"market", market},
		{"side", "ask"},
		{"ord_type", "market"},
		{"volume", strconv.FormatFloat(quantity, 'f', 8, 64)},
	}
}

// ── Adapter methods ──────────────────────────────────────────────────────

func (a *Adapter) PlaceOrder(ctx context.Context, creds trade.Creds, req trade.OpenRequest) (*trade.Result, error) {
	req.MarketType = trade.MarketSpot // spot-native — leverage/margin never apply
	if err := req.Validate(); err != nil {
		return nil, err
	}
	if !req.OrderType.EffectiveMarket() {
		return nil, errUser("upbit spot supports market orders only")
	}
	isBuy := req.Side == trade.SideBuy
	var px float64
	if isBuy {
		var err error
		px, err = a.lastPrice(ctx, toMarket(req.Symbol))
		if err != nil {
			return nil, err
		}
	}
	body, err := a.signedRequest(ctx, creds, http.MethodPost, "/v1/orders",
		orderParams(req.Symbol, isBuy, req.Quantity, px))
	if err != nil {
		return nil, err
	}
	var resp struct {
		UUID string `json:"uuid"`
	}
	_ = json.Unmarshal(body, &resp)

	avgPrice, filledQty := a.pollFill(ctx, creds, resp.UUID)
	qty := req.Quantity
	if filledQty > 0 {
		qty = filledQty
	}
	return &trade.Result{
		OrderID:   resp.UUID,
		Symbol:    req.Symbol,
		Side:      req.Side,
		Quantity:  qty,
		AvgPrice:  avgPrice,
		Status:    "NEW",
		CreatedAt: time.Now().UTC(),
		Raw:       body,
	}, nil
}

// pollFill — one shot 400ms after placement, mirrors Python. Market
// orders on Upbit fill near-instantly; a miss just leaves avg/qty at 0
// and the reconcile worker picks the real numbers up later.
func (a *Adapter) pollFill(ctx context.Context, creds trade.Creds, orderID string) (avgPrice, filledQty float64) {
	if orderID == "" {
		return 0, 0
	}
	select {
	case <-ctx.Done():
		return 0, 0
	case <-time.After(400 * time.Millisecond):
	}
	body, err := a.signedRequest(ctx, creds, http.MethodGet, "/v1/order",
		[]param{{"uuid", orderID}})
	if err != nil {
		return 0, 0
	}
	var od struct {
		Trades []struct {
			Funds  json.Number `json:"funds"`
			Volume json.Number `json:"volume"`
		} `json:"trades"`
	}
	if err := json.Unmarshal(body, &od); err != nil {
		return 0, 0
	}
	var spent, vol float64
	for _, t := range od.Trades {
		f, _ := t.Funds.Float64()
		v, _ := t.Volume.Float64()
		spent += f
		vol += v
	}
	if vol > 0 {
		return spent / vol, vol
	}
	return 0, 0
}

func (a *Adapter) ClosePosition(ctx context.Context, creds trade.Creds, req trade.CloseRequest) (*trade.Result, error) {
	if req.Symbol == "" {
		return nil, errUser("symbol required")
	}
	qty, err := a.baseBalance(ctx, creds, req.Symbol)
	if err != nil {
		return nil, err
	}
	if qty <= 0 {
		return &trade.Result{Symbol: req.Symbol, Side: trade.SideSell, Status: "FLAT"}, nil
	}
	return a.PlaceOrder(ctx, creds, trade.OpenRequest{
		Symbol:     req.Symbol,
		Side:       trade.SideSell,
		Quantity:   qty,
		MarketType: trade.MarketSpot,
	})
}

func (a *Adapter) baseBalance(ctx context.Context, creds trade.Creds, symbol string) (float64, error) {
	rows, err := a.accounts(ctx, creds)
	if err != nil {
		return 0, err
	}
	want := strings.ToUpper(symbol)
	for _, r := range rows {
		if strings.ToUpper(r.Currency) == want {
			qty, _ := r.Balance.Float64()
			return qty, nil
		}
	}
	return 0, nil
}

type account struct {
	Currency string      `json:"currency"`
	Balance  json.Number `json:"balance"`
	Locked   json.Number `json:"locked"`
}

func (a *Adapter) accounts(ctx context.Context, creds trade.Creds) ([]account, error) {
	body, err := a.signedRequest(ctx, creds, http.MethodGet, "/v1/accounts", nil)
	if err != nil {
		return nil, err
	}
	var rows []account
	if err := json.Unmarshal(body, &rows); err != nil {
		return nil, errInternal("parse accounts", err)
	}
	return rows, nil
}

func (a *Adapter) ListPositions(_ context.Context, _ trade.Creds, _ string) ([]trade.Position, error) {
	// Spot — holdings live in the balance snapshot, not positions.
	return []trade.Position{}, nil
}

func (a *Adapter) GetBalance(ctx context.Context, creds trade.Creds) (*trade.Balance, error) {
	rows, err := a.accounts(ctx, creds)
	if err != nil {
		return nil, err
	}
	var total, avail float64
	for _, r := range rows {
		ccy := strings.ToUpper(r.Currency)
		if ccy != "USDT" && ccy != "USDC" {
			continue
		}
		bal, _ := r.Balance.Float64()
		locked, _ := r.Locked.Float64()
		total += bal + locked
		avail += bal
	}
	return &trade.Balance{TotalUSD: total, AvailableUSD: avail}, nil
}

func (a *Adapter) SetLeverage(_ context.Context, _ trade.Creds, _ trade.LeverageRequest) error {
	// Spot — no leverage.
	return nil
}

// Spot routing — same paths; PlaceOrder/ClosePosition ARE the spot
// implementation on this venue.
func (a *Adapter) PlaceSpotOrder(ctx context.Context, creds trade.Creds, req trade.OpenRequest) (*trade.Result, error) {
	return a.PlaceOrder(ctx, creds, req)
}

func (a *Adapter) CloseSpotPosition(ctx context.Context, creds trade.Creds, req trade.CloseRequest) (*trade.Result, error) {
	return a.ClosePosition(ctx, creds, req)
}

func errUser(msg string, args ...any) *trade.Error {
	return &trade.Error{Kind: trade.KindUser, Message: fmt.Sprintf(msg, args...)}
}

func errInternal(msg string, cause error) *trade.Error {
	return &trade.Error{Kind: trade.KindInternal, Message: msg, Cause: cause}
}

var _ trade.Adapter = (*Adapter)(nil)
var _ trade.SpotAdapter = (*Adapter)(nil)
