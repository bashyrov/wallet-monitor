// WhiteBIT Futures (collateral) trade adapter.
//
// Port of `backend/services/trade_adapters/whitebit.py`.
//
// Signing: hex(HMAC-SHA512(secret, base64(json_body))). Headers:
//
//	X-TXC-APIKEY:    <api_key>
//	X-TXC-PAYLOAD:   <base64-json-body>
//	X-TXC-SIGNATURE: <hex digest>
//
// Body must include `request: <path>` and `nonce: <ms-timestamp>`.
// All requests are POST regardless of action — WhiteBIT uses path
// inside the body to multiplex.
//
// Quirks:
//   - Symbol form: "BTC_PERP".
//   - Amount in coins (no contract conversion).
//   - SetLeverage is a no-op (no public API).
package whitebit

import (
	"context"
	"crypto/sha512"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

const baseURL = "https://whitebit.com"

type Adapter struct {
	httpClient *http.Client
}

func New() *Adapter {
	return &Adapter{
		httpClient: &http.Client{
			Timeout: 15 * time.Second,
			Transport: &http.Transport{
				MaxIdleConnsPerHost: 8,
				IdleConnTimeout:     60 * time.Second,
			},
		},
	}
}

func init() { trade.Register("whitebit", New()) }

func (a *Adapter) Name() string { return "whitebit" }

func toWBSymbol(sym string) string { return strings.ToUpper(sym) + "_PERP" }

// ── Signing ──────────────────────────────────────────────────────────────

func (a *Adapter) signedRequest(
	ctx context.Context, creds trade.Creds, path string, body map[string]any,
) (json.RawMessage, error) {
	if body == nil {
		body = map[string]any{}
	}
	body["request"] = path
	body["nonce"] = time.Now().UnixMilli()

	bodyBytes, err := json.Marshal(body)
	if err != nil {
		return nil, errInternal("marshal body", err)
	}
	payloadB64 := base64.StdEncoding.EncodeToString(bodyBytes)
	sig := hmacHexSHA512(creds.APISecret, payloadB64)

	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		baseURL+path, strings.NewReader(string(bodyBytes)))
	if err != nil {
		return nil, err
	}
	req.Header.Set("X-TXC-APIKEY", creds.APIKey)
	req.Header.Set("X-TXC-PAYLOAD", payloadB64)
	req.Header.Set("X-TXC-SIGNATURE", sig)
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
	return raw, nil
}

func hmacHexSHA512(secret, payload string) string {
	return hex.EncodeToString(trade.HMACWith(sha512.New, secret, payload))
}

func parseError(status int, body []byte) *trade.Error {
	var env struct {
		Code    int    `json:"code"`
		Message string `json:"message"`
	}
	_ = json.Unmarshal(body, &env)
	msg := env.Message
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
	body, err := a.signedRequest(ctx, creds, "/api/v4/trade-account/balance", nil)
	if err != nil {
		return nil, err
	}
	var data map[string]struct {
		Available string `json:"available"`
		Balance   string `json:"balance"`
	}
	if err := json.Unmarshal(body, &data); err != nil {
		return nil, errInternal("parse balance", err)
	}
	if u, ok := data["USDT"]; ok {
		avail, _ := strconv.ParseFloat(u.Available, 64)
		total, _ := strconv.ParseFloat(u.Balance, 64)
		if avail == 0 {
			avail = total
		}
		return &trade.Balance{TotalUSD: total, AvailableUSD: avail}, nil
	}
	return &trade.Balance{}, nil
}

func (a *Adapter) SetLeverage(_ context.Context, _ trade.Creds, _ trade.LeverageRequest) error {
	// WhiteBIT has no public per-symbol set-leverage endpoint —
	// leverage is configured account-wide via the web UI.
	return nil
}

func (a *Adapter) PlaceOrder(ctx context.Context, creds trade.Creds, req trade.OpenRequest) (*trade.Result, error) {
	if err := req.Validate(); err != nil {
		return nil, err
	}
	side := "buy"
	if req.Side == trade.SideSell {
		side = "sell"
	}
	body, err := a.signedRequest(ctx, creds, "/api/v4/order/collateral/market",
		map[string]any{
			"market": toWBSymbol(req.Symbol),
			"side":   side,
			"amount": qtyString(req.Quantity),
		})
	if err != nil {
		return nil, err
	}
	var resp struct {
		OrderID json.Number `json:"orderId"`
		ID      json.Number `json:"id"`
		DealMoney string    `json:"dealMoney"`
	}
	_ = json.Unmarshal(body, &resp)
	id := string(resp.OrderID)
	if id == "" {
		id = string(resp.ID)
	}
	avg, _ := strconv.ParseFloat(resp.DealMoney, 64)
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
	positions, err := a.ListPositions(ctx, creds, req.Symbol)
	if err != nil {
		return nil, err
	}
	if len(positions) == 0 {
		return &trade.Result{Symbol: req.Symbol, Status: "FLAT"}, nil
	}
	p := positions[0]
	reduceSide := "sell"
	if p.Side == trade.SideSell {
		reduceSide = "buy"
	}
	body, err := a.signedRequest(ctx, creds, "/api/v4/order/collateral/market",
		map[string]any{
			"market":     toWBSymbol(req.Symbol),
			"side":       reduceSide,
			"amount":     qtyString(p.Quantity),
			"reduceOnly": true,
		})
	if err != nil {
		return nil, err
	}
	var resp struct {
		OrderID json.Number `json:"orderId"`
		ID      json.Number `json:"id"`
	}
	_ = json.Unmarshal(body, &resp)
	id := string(resp.OrderID)
	if id == "" {
		id = string(resp.ID)
	}
	closeSide := trade.SideSell
	if reduceSide == "buy" {
		closeSide = trade.SideBuy
	}
	return &trade.Result{
		OrderID:   id,
		Symbol:    req.Symbol,
		Side:      closeSide,
		Quantity:  p.Quantity,
		Status:    "NEW",
		CreatedAt: time.Now().UTC(),
		Raw:       body,
	}, nil
}

func (a *Adapter) ListPositions(ctx context.Context, creds trade.Creds, symbol string) ([]trade.Position, error) {
	body, err := a.signedRequest(ctx, creds, "/api/v4/collateral-account/positions/open", nil)
	if err != nil {
		return nil, err
	}
	var rows []struct {
		Market         string      `json:"market"`
		Amount         json.Number `json:"amount"`
		BaseAmount     json.Number `json:"baseAmount"`
		EntryPrice     json.Number `json:"entryPrice"`
		BasePrice      json.Number `json:"basePrice"`
		MarkPrice      json.Number `json:"markPrice"`
		CurrentPrice   json.Number `json:"currentPrice"`
		UnrealizedPnL  json.Number `json:"unrealizedPnl"`
		Pnl            json.Number `json:"pnl"`
		Leverage       json.Number `json:"leverage"`
	}
	if err := json.Unmarshal(body, &rows); err != nil {
		return nil, errInternal("parse positions", err)
	}
	wantSym := strings.ToUpper(symbol)
	out := make([]trade.Position, 0, len(rows))
	for _, p := range rows {
		amt, _ := p.Amount.Float64()
		if amt == 0 {
			amt, _ = p.BaseAmount.Float64()
		}
		if amt == 0 {
			continue
		}
		baseSym := strings.TrimSuffix(p.Market, "_PERP")
		if wantSym != "" && baseSym != wantSym {
			continue
		}
		side := trade.SideBuy
		if amt < 0 {
			side = trade.SideSell
		}
		entry, _ := p.EntryPrice.Float64()
		if entry == 0 {
			entry, _ = p.BasePrice.Float64()
		}
		mark, _ := p.MarkPrice.Float64()
		if mark == 0 {
			mark, _ = p.CurrentPrice.Float64()
		}
		upl, _ := p.UnrealizedPnL.Float64()
		if upl == 0 {
			upl, _ = p.Pnl.Float64()
		}
		lev, _ := p.Leverage.Float64()
		out = append(out, trade.Position{
			Symbol:        baseSym,
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
