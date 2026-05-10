// Kraken Futures (futures.kraken.com /derivatives/api/v3) trade adapter.
//
// Port of `backend/services/trade_adapters/kraken.py`.
//
// Signing flavour (most exotic of the bunch):
//
//	hash    = SHA256(post_data + nonce + path)
//	authent = base64(HMAC_SHA512( base64_decode(api_secret), hash ))
//
// Headers:
//
//	APIKey:   <api_key>
//	Nonce:    <ms-timestamp>
//	Authent:  <base64 sig>
//
// Quirks:
//   - Symbol form: "PF_BTCUSD" (BTC mapped to XBT historic naming).
//   - Quantity in COINS (no contract conversion).
//   - One endpoint family: /derivatives/api/v3 prefixes the URL but
//     the SIGNATURE uses the path WITHOUT it (Kraken docs trap).
//   - SetLeverage hits /leveragepreferences; venue silently accepts
//     "already at this value" so we treat any error as soft.
package kraken

import (
	"context"
	"crypto/sha256"
	"crypto/sha512"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

const (
	baseURL = "https://futures.kraken.com"
	root    = "/derivatives/api/v3"
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

func init() { trade.Register("kraken", New()) }

func (a *Adapter) Name() string { return "kraken" }

// ── Symbol mapping ───────────────────────────────────────────────────────

func toKrakenSymbol(sym string) string {
	s := strings.ToUpper(sym)
	if s == "BTC" {
		s = "XBT"
	}
	return "PF_" + s + "USD"
}

func fromKrakenSymbol(pf string) string {
	s := strings.TrimPrefix(pf, "PF_")
	s = strings.TrimSuffix(s, "USD")
	if s == "XBT" {
		s = "BTC"
	}
	return s
}

// ── Signing ──────────────────────────────────────────────────────────────

func krakenSign(apiSecret, postData, nonce, path string) (string, error) {
	secret, err := base64.StdEncoding.DecodeString(apiSecret)
	if err != nil {
		return "", err
	}
	preHash := sha256.Sum256([]byte(postData + nonce + path))
	mac := trade.HMACWith(sha512.New, string(secret), string(preHash[:]))
	return base64.StdEncoding.EncodeToString(mac), nil
}

func (a *Adapter) signedRequest(
	ctx context.Context, creds trade.Creds, method, path string,
	params map[string]string,
) (json.RawMessage, error) {
	postData := ""
	if len(params) > 0 {
		postData = url.Values{}.Encode() // start empty
		v := url.Values{}
		for k, val := range params {
			v.Set(k, val)
		}
		postData = v.Encode()
	}
	nonce := strconv.FormatInt(time.Now().UnixMilli(), 10)
	authent, err := krakenSign(creds.APISecret, postData, nonce, path)
	if err != nil {
		return nil, errInternal("base64 secret decode", err)
	}

	u := baseURL + root + path
	var bodyReader io.Reader
	if method == http.MethodGet && postData != "" {
		u += "?" + postData
	} else if method != http.MethodGet && postData != "" {
		bodyReader = strings.NewReader(postData)
	}
	req, err := http.NewRequestWithContext(ctx, method, u, bodyReader)
	if err != nil {
		return nil, err
	}
	req.Header.Set("APIKey", creds.APIKey)
	req.Header.Set("Nonce", nonce)
	req.Header.Set("Authent", authent)
	req.Header.Set("Accept", "application/json")
	if method != http.MethodGet {
		req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
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
		Result string `json:"result"`
		Error  string `json:"error"`
	}
	_ = json.Unmarshal(raw, &env)
	if env.Result == "error" {
		return nil, &trade.Error{Kind: trade.KindExchange, Message: env.Error}
	}
	return raw, nil
}

func parseError(status int, body []byte) *trade.Error {
	var env struct {
		Error string `json:"error"`
	}
	_ = json.Unmarshal(body, &env)
	msg := env.Error
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
	body, err := a.signedRequest(ctx, creds, http.MethodGet, "/accounts", nil)
	if err != nil {
		return nil, err
	}
	var env struct {
		Accounts struct {
			Flex struct {
				BalanceValue json.Number `json:"balanceValue"`
			} `json:"flex"`
			Cash struct {
				Balances map[string]json.Number `json:"balances"`
			} `json:"cash"`
		} `json:"accounts"`
	}
	if err := json.Unmarshal(body, &env); err != nil {
		return nil, errInternal("parse balance", err)
	}
	usdt, _ := env.Accounts.Flex.BalanceValue.Float64()
	if usdt == 0 {
		usd, _ := env.Accounts.Cash.Balances["USD"].Float64()
		usdtBal, _ := env.Accounts.Cash.Balances["USDT"].Float64()
		usdt = usd + usdtBal
	}
	return &trade.Balance{TotalUSD: usdt, AvailableUSD: usdt}, nil
}

func (a *Adapter) SetLeverage(ctx context.Context, creds trade.Creds, req trade.LeverageRequest) error {
	if req.Leverage <= 0 {
		return errUser("leverage must be > 0")
	}
	_, err := a.signedRequest(ctx, creds, http.MethodPost,
		"/leveragepreferences", map[string]string{
			"symbol":      toKrakenSymbol(req.Symbol),
			"maxLeverage": strconv.Itoa(req.Leverage),
		})
	if err != nil {
		// Already-set / not-modified — Kraken returns various error
		// strings. Treat all set-leverage errors as soft (the order
		// can still proceed at whatever leverage is currently set).
		return nil
	}
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
	body, err := a.signedRequest(ctx, creds, http.MethodPost, "/sendorder",
		map[string]string{
			"orderType": "mkt",
			"symbol":    toKrakenSymbol(req.Symbol),
			"side":      side,
			"size":      qtyString(req.Quantity),
		})
	if err != nil {
		return nil, err
	}
	var resp struct {
		SendStatus struct {
			OrderID    string `json:"order_id"`
			FillStatus struct {
				Price json.Number `json:"price"`
			} `json:"fillStatus"`
		} `json:"sendStatus"`
	}
	_ = json.Unmarshal(body, &resp)
	avg, _ := resp.SendStatus.FillStatus.Price.Float64()
	return &trade.Result{
		OrderID:   resp.SendStatus.OrderID,
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
	body, err := a.signedRequest(ctx, creds, http.MethodPost, "/sendorder",
		map[string]string{
			"orderType":  "mkt",
			"symbol":     toKrakenSymbol(req.Symbol),
			"side":       reduceSide,
			"size":       qtyString(p.Quantity),
			"reduceOnly": "true",
		})
	if err != nil {
		return nil, err
	}
	var resp struct {
		SendStatus struct {
			OrderID string `json:"order_id"`
		} `json:"sendStatus"`
	}
	_ = json.Unmarshal(body, &resp)
	closeSide := trade.SideSell
	if reduceSide == "buy" {
		closeSide = trade.SideBuy
	}
	return &trade.Result{
		OrderID:   resp.SendStatus.OrderID,
		Symbol:    req.Symbol,
		Side:      closeSide,
		Quantity:  p.Quantity,
		Status:    "NEW",
		CreatedAt: time.Now().UTC(),
		Raw:       body,
	}, nil
}

func (a *Adapter) ListPositions(ctx context.Context, creds trade.Creds, symbol string) ([]trade.Position, error) {
	body, err := a.signedRequest(ctx, creds, http.MethodGet, "/openpositions", nil)
	if err != nil {
		return nil, err
	}
	var env struct {
		OpenPositions []struct {
			Symbol      string      `json:"symbol"`
			Side        string      `json:"side"` // long / short
			Size        json.Number `json:"size"`
			Price       json.Number `json:"price"` // entry
			MarkPrice   json.Number `json:"markPrice"`
			UnrealizedFunding json.Number `json:"unrealizedFunding"`
			MaxLeverage json.Number `json:"maxLeverage"`
		} `json:"openPositions"`
	}
	if err := json.Unmarshal(body, &env); err != nil {
		return nil, errInternal("parse positions", err)
	}
	wantSym := strings.ToUpper(symbol)
	out := make([]trade.Position, 0, len(env.OpenPositions))
	for _, p := range env.OpenPositions {
		base := fromKrakenSymbol(p.Symbol)
		if wantSym != "" && base != wantSym {
			continue
		}
		size, _ := p.Size.Float64()
		if size == 0 {
			continue
		}
		side := trade.SideBuy
		if strings.EqualFold(p.Side, "short") {
			side = trade.SideSell
		}
		entry, _ := p.Price.Float64()
		mark, _ := p.MarkPrice.Float64()
		fund, _ := p.UnrealizedFunding.Float64()
		lev, _ := p.MaxLeverage.Float64()
		out = append(out, trade.Position{
			Symbol:        base,
			Side:          side,
			Quantity:      math.Abs(size),
			EntryPrice:    entry,
			MarkPrice:     mark,
			Leverage:      int(lev),
			UnrealizedPnL: fund, // Kraken's flex collateral mode
			Notional:      math.Abs(size) * mark,
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
