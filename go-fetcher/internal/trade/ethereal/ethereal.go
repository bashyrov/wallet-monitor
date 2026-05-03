// Ethereal DEX trade adapter — linked-signer auth via personal_sign.
//
// Port of `backend/services/trade_adapters/ethereal.py`.
//
// Auth: a "Linked Signer" (any ETH keypair) that the user has bound to
// their subaccount on ethereal.trade. The signer can trade but cannot
// withdraw. We do NOT submit EIP-712 typed data — Python's adapter
// uses the simpler personal_sign over a flat string:
//
//	sign_payload = METHOD || PATH || TIMESTAMP_NS || JSON(body)
//	sig          = personal_sign(sign_payload, linkedSignerKey)
//
// Headers
//
//	X-Ethereal-Address:   <subaccount addr>
//	X-Ethereal-Timestamp: <ns>
//	X-Ethereal-Signature: <0x-prefixed 65-byte hex>
//
// Quirks
//
//   - Timestamp is in NANOseconds.
//   - GET /v1/subaccount?address=… is unsigned (public read).
//   - Public WS isn't usable; the adapter drives only trade actions.
package ethereal

import (
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/ethereum/go-ethereum/crypto"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

const baseURL = "https://api.ethereal.trade"

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

func init() { trade.Register("ethereal", New()) }

func (a *Adapter) Name() string { return "ethereal" }

// ── Signing ──────────────────────────────────────────────────────────────

// signPersonal returns the 0x-prefixed personal_sign hex for the given
// payload. Mirrors `eth_account.messages.encode_defunct(text=...)`
// followed by `Account.sign_message`.
func signPersonal(payload, privKeyHex string) (string, error) {
	priv, err := crypto.HexToECDSA(strings.TrimPrefix(privKeyHex, "0x"))
	if err != nil {
		return "", fmt.Errorf("parse private key: %w", err)
	}
	prefix := []byte(fmt.Sprintf("\x19Ethereum Signed Message:\n%d", len(payload)))
	msg := append(prefix, []byte(payload)...)
	digest := crypto.Keccak256(msg)
	sig, err := crypto.Sign(digest, priv)
	if err != nil {
		return "", fmt.Errorf("sign: %w", err)
	}
	if len(sig) != 65 {
		return "", fmt.Errorf("unexpected sig length %d", len(sig))
	}
	sig[64] += 27
	return "0x" + hex.EncodeToString(sig), nil
}

// signedRequest authenticates with the linked-signer key. GETs that
// don't take auth (public reads) call doRequest directly.
func (a *Adapter) signedRequest(
	ctx context.Context, creds trade.Creds, method, path string,
	body map[string]any,
) (json.RawMessage, error) {
	if creds.APISecret == "" {
		return nil, errUser("linked signer private key required")
	}
	tsNs := strconv.FormatInt(time.Now().UnixNano(), 10)

	bodyBytes := []byte("{}")
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return nil, errInternal("marshal body", err)
		}
		bodyBytes = b
	}
	payload := method + path + tsNs + string(bodyBytes)
	sig, err := signPersonal(payload, creds.APISecret)
	if err != nil {
		return nil, errInternal("personal sign", err)
	}

	address := creds.APIKey
	if address == "" {
		address = creds.Wallet
	}

	var bodyReader io.Reader
	if body != nil {
		bodyReader = bytes.NewReader(bodyBytes)
	}
	req, err := http.NewRequestWithContext(ctx, method, baseURL+path, bodyReader)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Ethereal-Address", address)
	req.Header.Set("X-Ethereal-Timestamp", tsNs)
	req.Header.Set("X-Ethereal-Signature", sig)

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

func (a *Adapter) publicGet(ctx context.Context, path string, qs url.Values) (json.RawMessage, error) {
	u := baseURL + path
	if len(qs) > 0 {
		u += "?" + qs.Encode()
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return nil, err
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
	return raw, nil
}

func parseError(status int, body []byte) *trade.Error {
	var env struct {
		Error   string `json:"error"`
		Message string `json:"message"`
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
	address := creds.APIKey
	if address == "" {
		address = creds.Wallet
	}
	if address == "" {
		return nil, errUser("subaccount address required")
	}
	body, err := a.publicGet(ctx, "/v1/subaccount", url.Values{"address": []string{address}})
	if err != nil {
		return nil, err
	}
	var resp struct {
		Equity       json.Number `json:"equity"`
		AccountValue json.Number `json:"accountValue"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		return nil, errInternal("parse subaccount", err)
	}
	v, _ := resp.Equity.Float64()
	if v == 0 {
		v, _ = resp.AccountValue.Float64()
	}
	return &trade.Balance{TotalUSD: v, AvailableUSD: v}, nil
}

func (a *Adapter) SetLeverage(_ context.Context, _ trade.Creds, _ trade.LeverageRequest) error {
	return nil // Ethereal sets leverage at order time, not as a separate call.
}

func (a *Adapter) PlaceOrder(ctx context.Context, creds trade.Creds, req trade.OpenRequest) (*trade.Result, error) {
	if err := req.Validate(); err != nil {
		return nil, err
	}
	body, err := a.signedRequest(ctx, creds, http.MethodPost, "/v1/order", map[string]any{
		"symbol":   strings.ToUpper(req.Symbol),
		"side":     string(req.Side),
		"type":     "market",
		"quantity": qtyString(req.Quantity),
	})
	if err != nil {
		return nil, err
	}
	id := extractOrderID(body)
	return &trade.Result{
		OrderID:   id,
		Symbol:    req.Symbol,
		Side:      req.Side,
		Quantity:  req.Quantity,
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
	closeSide := trade.SideSell
	if p.Side == trade.SideSell {
		closeSide = trade.SideBuy
	}
	body, err := a.signedRequest(ctx, creds, http.MethodPost, "/v1/order", map[string]any{
		"symbol":     strings.ToUpper(req.Symbol),
		"side":       string(closeSide),
		"type":       "market",
		"quantity":   qtyString(p.Quantity),
		"reduceOnly": true,
	})
	if err != nil {
		return nil, err
	}
	id := extractOrderID(body)
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
	address := creds.APIKey
	if address == "" {
		address = creds.Wallet
	}
	if address == "" {
		return nil, errUser("subaccount address required")
	}
	body, err := a.publicGet(ctx, "/v1/subaccount", url.Values{"address": []string{address}})
	if err != nil {
		return nil, err
	}
	var resp struct {
		Positions []struct {
			Symbol         string      `json:"symbol"`
			ProductSymbol  string      `json:"productSymbol"`
			Size           json.Number `json:"size"`
			EntryPrice     json.Number `json:"entryPrice"`
			MarkPrice      json.Number `json:"markPrice"`
			UnrealizedPnl  json.Number `json:"unrealizedPnl"`
		} `json:"positions"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		return nil, errInternal("parse positions", err)
	}
	want := strings.ToUpper(symbol)
	out := make([]trade.Position, 0, len(resp.Positions))
	for _, p := range resp.Positions {
		sz, _ := p.Size.Float64()
		if sz == 0 {
			continue
		}
		sym := p.Symbol
		if sym == "" {
			sym = p.ProductSymbol
		}
		if want != "" && strings.ToUpper(sym) != want {
			continue
		}
		side := trade.SideBuy
		if sz < 0 {
			side = trade.SideSell
		}
		entry, _ := p.EntryPrice.Float64()
		mark, _ := p.MarkPrice.Float64()
		upnl, _ := p.UnrealizedPnl.Float64()
		out = append(out, trade.Position{
			Symbol:        sym,
			Side:          side,
			Quantity:      abs(sz),
			EntryPrice:    entry,
			MarkPrice:     mark,
			UnrealizedPnL: upnl,
			Leverage:      1,
		})
	}
	return out, nil
}

// ── Helpers ──────────────────────────────────────────────────────────────

// extractOrderID handles both `{"orderId": "..."}` and `{"id": "..."}`
// (Python adapter falls back to `id` if `orderId` is absent).
func extractOrderID(body []byte) string {
	var resp struct {
		OrderID json.RawMessage `json:"orderId"`
		ID      json.RawMessage `json:"id"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		return ""
	}
	if id := unquoteOrNumber(resp.OrderID); id != "" {
		return id
	}
	return unquoteOrNumber(resp.ID)
}

func unquoteOrNumber(raw json.RawMessage) string {
	s := strings.TrimSpace(string(raw))
	if s == "" || s == "null" {
		return ""
	}
	if strings.HasPrefix(s, `"`) && strings.HasSuffix(s, `"`) && len(s) >= 2 {
		return s[1 : len(s)-1]
	}
	return s
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

func abs(f float64) float64 {
	if f < 0 {
		return -f
	}
	return f
}

func errUser(msg string, args ...any) *trade.Error {
	return &trade.Error{Kind: trade.KindUser, Message: fmt.Sprintf(msg, args...)}
}

func errInternal(msg string, cause error) *trade.Error {
	return &trade.Error{Kind: trade.KindInternal, Message: msg, Cause: cause}
}

var _ trade.Adapter = (*Adapter)(nil)
