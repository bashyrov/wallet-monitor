// Aster DEX trade adapter — EIP-712 API-wallet signing on a
// Binance-fork FAPI surface.
//
// Port of `backend/services/trade_adapters/aster.py`.
//
// Signing: EIP-712 typed data
//
//	domain  = { name: "AsterSignTransaction", chainId: 1666 }
//	type    = AsterSignTransaction(string params)
//	message = { params: <sorted-query-string> }
//
// The signer secret is the API-wallet private key (`creds.api_secret`,
// hex with or without `0x`). The API-wallet public address goes into
// `X-AB-APIKEY`.
//
// Wire shape ≈ Binance FAPI (same endpoints, `BTCUSDT` symbol form).
//
// Quirks
//
//   - Timestamp is in MICROseconds (not ms). Aster's signer rejects ms.
//   - All signed POST/DELETE encode params in the URL (no body), exactly
//     like Binance — but the value placed under `signature=` is the
//     EIP-712 ECDSA hex (130+2 chars) instead of an HMAC.
//   - Aster's funding income lives at /fapi/v1/income (Binance shape).
package aster

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"math/big"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/ethereum/go-ethereum/common/math"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/signer/core/apitypes"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

// Nonce counter — monotonic-by-second + per-second counter. Matches the
// Python adapter's _next_nonce() so signatures stay consistent if both
// signers run in parallel against the same account.
var (
	_nonceMu      sync.Mutex
	_nonceLast    int64
	_nonceCounter int64
)

func nextNonce() int64 {
	_nonceMu.Lock()
	defer _nonceMu.Unlock()
	now := time.Now().Unix()
	if now == _nonceLast {
		_nonceCounter++
	} else {
		_nonceLast = now
		_nonceCounter = 0
	}
	return now*1_000_000 + _nonceCounter
}

const (
	baseURL = "https://fapi.asterdex.com"
	chainID = 1666
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

func init() { trade.Register("aster", New()) }

func (a *Adapter) Name() string { return "aster" }

func toAsterSymbol(sym string) string { return strings.ToUpper(sym) + "USDT" }

// ── Signing ──────────────────────────────────────────────────────────────

// buildQueryString builds the sorted query string that goes into both
// the URL and the EIP-712 message.params field. Aster's signer is
// strict about ordering — sort keys, no urlencode (Binance convention).
func buildQueryString(params map[string]string) string {
	keys := make([]string, 0, len(params))
	for k := range params {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, k+"="+params[k])
	}
	return strings.Join(parts, "&")
}

// signEIP712 returns the 0x-prefixed signature hex for Aster's V3
// Message(string msg) typed data.
//
// Domain must match Python's aster.py exactly (version + verifyingContract
// fields are required by the venue's verifier; older 2-field domain triggers
// "Signature check failed"). The signed `msg` is the URL-encoded body
// including the nonce/user/signer triplet.
func signEIP712(qs, privKeyHex string) (string, error) {
	td := apitypes.TypedData{
		Types: apitypes.Types{
			"EIP712Domain": []apitypes.Type{
				{Name: "name", Type: "string"},
				{Name: "version", Type: "string"},
				{Name: "chainId", Type: "uint256"},
				{Name: "verifyingContract", Type: "address"},
			},
			"Message": []apitypes.Type{
				{Name: "msg", Type: "string"},
			},
		},
		PrimaryType: "Message",
		Domain: apitypes.TypedDataDomain{
			Name:              "AsterSignTransaction",
			Version:           "1",
			ChainId:           math.NewHexOrDecimal256(chainID),
			VerifyingContract: "0x0000000000000000000000000000000000000000",
		},
		Message: apitypes.TypedDataMessage{
			"msg": qs,
		},
	}

	domainSep, err := td.HashStruct("EIP712Domain", td.Domain.Map())
	if err != nil {
		return "", fmt.Errorf("eip712 domain: %w", err)
	}
	msgHash, err := td.HashStruct(td.PrimaryType, td.Message)
	if err != nil {
		return "", fmt.Errorf("eip712 struct: %w", err)
	}

	raw := make([]byte, 0, 2+len(domainSep)+len(msgHash))
	raw = append(raw, 0x19, 0x01)
	raw = append(raw, domainSep...)
	raw = append(raw, msgHash...)
	digest := crypto.Keccak256(raw)

	priv, err := crypto.HexToECDSA(strings.TrimPrefix(privKeyHex, "0x"))
	if err != nil {
		return "", fmt.Errorf("parse private key: %w", err)
	}
	sig, err := crypto.Sign(digest, priv)
	if err != nil {
		return "", fmt.Errorf("sign: %w", err)
	}
	if len(sig) != 65 {
		return "", fmt.Errorf("unexpected sig length %d", len(sig))
	}
	// crypto.Sign returns v ∈ {0,1}; Ethereum personal/typed signatures
	// expect 27/28.
	sig[64] += 27
	return "0x" + hex.EncodeToString(sig), nil
}

// signedRequest signs `params` via EIP-712 and dispatches the request.
// Body is always empty on Aster — params ride in the URL like Binance.
//
// Aster V3 EIP-712 signing requires three additional fields beyond the user
// params: nonce (monotonic counter), user (master wallet address = APIKey),
// signer (address derived from APISecret = API-wallet private key). The
// signed message is the urlencoded sorted query string including all of these.
func (a *Adapter) signedRequest(
	ctx context.Context, creds trade.Creds, method, path string, params map[string]string,
) (json.RawMessage, error) {
	if creds.APIKey == "" || creds.APISecret == "" {
		return nil, errUser("aster requires both api_key (master address) and api_secret (api-wallet private key)")
	}
	priv, err := crypto.HexToECDSA(strings.TrimPrefix(creds.APISecret, "0x"))
	if err != nil {
		return nil, errUser("aster api_secret invalid hex: %v", err)
	}
	signerAddr := crypto.PubkeyToAddress(priv.PublicKey).Hex()

	if params == nil {
		params = map[string]string{}
	}
	params["nonce"] = strconv.FormatInt(nextNonce(), 10)
	params["user"] = creds.APIKey
	params["signer"] = signerAddr

	qs := buildQueryString(params)
	sig, err := signEIP712(qs, creds.APISecret)
	if err != nil {
		return nil, errInternal("eip712 sign", err)
	}

	u := baseURL + path + "?" + qs + "&signature=" + sig

	req, err := http.NewRequestWithContext(ctx, method, u, nil)
	if err != nil {
		return nil, err
	}
	// V3 API doesn't accept X-AB-APIKEY (V1 only); the venue identifies the
	// account from the `user` field in the signed msg. Setting the header
	// triggers "API-key format invalid" on the V3 endpoints.

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
		Code int    `json:"code"`
		Msg  string `json:"msg"`
	}
	_ = json.Unmarshal(body, &env)
	msg := env.Msg
	if msg == "" {
		msg = strings.TrimSpace(string(body))
	}
	if status == 429 || env.Code == -1003 {
		return &trade.Error{Kind: trade.KindRateLimit, Message: msg, Code: strconv.Itoa(env.Code)}
	}
	return &trade.Error{Kind: trade.KindExchange, Message: msg, Code: strconv.Itoa(env.Code)}
}

// ── Adapter methods ──────────────────────────────────────────────────────

func (a *Adapter) GetBalance(ctx context.Context, creds trade.Creds) (*trade.Balance, error) {
	body, err := a.signedRequest(ctx, creds, http.MethodGet, "/fapi/v3/balance", nil)
	if err != nil {
		return nil, err
	}
	var rows []struct {
		Asset            string `json:"asset"`
		Balance          string `json:"balance"`
		AvailableBalance string `json:"availableBalance"`
	}
	if err := json.Unmarshal(body, &rows); err != nil {
		return nil, errInternal("parse balance", err)
	}
	for _, r := range rows {
		if strings.ToUpper(r.Asset) == "USDT" {
			total, _ := strconv.ParseFloat(r.Balance, 64)
			avail, _ := strconv.ParseFloat(r.AvailableBalance, 64)
			return &trade.Balance{TotalUSD: total, AvailableUSD: avail}, nil
		}
	}
	return &trade.Balance{}, nil
}

func (a *Adapter) SetLeverage(ctx context.Context, creds trade.Creds, req trade.LeverageRequest) error {
	if req.Symbol == "" {
		return errUser("symbol required")
	}
	if req.Leverage <= 0 {
		return errUser("leverage must be > 0")
	}
	sym := toAsterSymbol(req.Symbol)
	mode := "CROSSED"
	if req.MarginMode == trade.MarginIsolated {
		mode = "ISOLATED"
	}
	// marginType returns -4046 ("No need to change margin type") if
	// already set — same as Binance. Treat as success.
	if _, err := a.signedRequest(ctx, creds, http.MethodPost, "/fapi/v3/marginType",
		map[string]string{"symbol": sym, "marginType": mode}); err != nil {
		if !isAlreadySet(err) {
			return err
		}
	}
	if _, err := a.signedRequest(ctx, creds, http.MethodPost, "/fapi/v3/leverage",
		map[string]string{"symbol": sym, "leverage": strconv.Itoa(req.Leverage)}); err != nil {
		return err
	}
	return nil
}

func isAlreadySet(err error) bool {
	if e, ok := err.(*trade.Error); ok {
		return strings.Contains(e.Message, "No need") || e.Code == "-4046"
	}
	return false
}

func (a *Adapter) PlaceOrder(ctx context.Context, creds trade.Creds, req trade.OpenRequest) (*trade.Result, error) {
	if err := req.Validate(); err != nil {
		return nil, err
	}
	side := "BUY"
	if req.Side == trade.SideSell {
		side = "SELL"
	}
	orderParams := map[string]string{
		"symbol":   toAsterSymbol(req.Symbol),
		"side":     side,
		"quantity": qtyString(req.Quantity),
	}
	switch req.OrderType {
	case trade.OrderLimit:
		orderParams["type"] = "LIMIT"
		orderParams["price"] = strconv.FormatFloat(req.LimitPrice, 'f', -1, 64)
		orderParams["timeInForce"] = "GTC"
	case trade.OrderStopMarket:
		orderParams["type"] = "STOP_MARKET"
		orderParams["stopPrice"] = strconv.FormatFloat(req.StopPrice, 'f', -1, 64)
	case trade.OrderTakeProfitMkt:
		orderParams["type"] = "TAKE_PROFIT_MARKET"
		orderParams["stopPrice"] = strconv.FormatFloat(req.StopPrice, 'f', -1, 64)
	default:
		orderParams["type"] = "MARKET"
	}
	body, err := a.signedRequest(ctx, creds, http.MethodPost, "/fapi/v3/order", orderParams)
	if err != nil {
		return nil, err
	}
	var resp struct {
		OrderID  json.Number `json:"orderId"`
		AvgPrice string      `json:"avgPrice"`
	}
	_ = json.Unmarshal(body, &resp)
	avg, _ := strconv.ParseFloat(resp.AvgPrice, 64)
	return &trade.Result{
		OrderID:   resp.OrderID.String(),
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
	reduceSide := "SELL"
	closeSide := trade.SideSell
	if p.Side == trade.SideSell {
		reduceSide = "BUY"
		closeSide = trade.SideBuy
	}
	body, err := a.signedRequest(ctx, creds, http.MethodPost, "/fapi/v3/order",
		map[string]string{
			"symbol":       toAsterSymbol(req.Symbol),
			"side":         reduceSide,
			"type":         "MARKET",
			"quantity":     qtyString(p.Quantity),
			"reduceOnly":   "true",
		})
	if err != nil {
		return nil, err
	}
	var resp struct {
		OrderID json.Number `json:"orderId"`
	}
	_ = json.Unmarshal(body, &resp)
	return &trade.Result{
		OrderID:   resp.OrderID.String(),
		Symbol:    req.Symbol,
		Side:      closeSide,
		Quantity:  p.Quantity,
		Status:    "NEW",
		CreatedAt: time.Now().UTC(),
		Raw:       body,
	}, nil
}

func (a *Adapter) ListPositions(ctx context.Context, creds trade.Creds, symbol string) ([]trade.Position, error) {
	params := map[string]string{}
	if symbol != "" {
		params["symbol"] = toAsterSymbol(symbol)
	}
	body, err := a.signedRequest(ctx, creds, http.MethodGet, "/fapi/v3/positionRisk", params)
	if err != nil {
		return nil, err
	}
	var rows []struct {
		Symbol           string `json:"symbol"`
		PositionAmt      string `json:"positionAmt"`
		EntryPrice       string `json:"entryPrice"`
		MarkPrice        string `json:"markPrice"`
		UnRealizedProfit string `json:"unRealizedProfit"`
		Leverage         string `json:"leverage"`
	}
	if err := json.Unmarshal(body, &rows); err != nil {
		return nil, errInternal("parse positions", err)
	}
	out := make([]trade.Position, 0, len(rows))
	for _, r := range rows {
		amt, _ := strconv.ParseFloat(r.PositionAmt, 64)
		if amt == 0 {
			continue
		}
		side := trade.SideBuy
		if amt < 0 {
			side = trade.SideSell
		}
		entry, _ := strconv.ParseFloat(r.EntryPrice, 64)
		mark, _ := strconv.ParseFloat(r.MarkPrice, 64)
		upnl, _ := strconv.ParseFloat(r.UnRealizedProfit, 64)
		levF, _ := strconv.ParseFloat(r.Leverage, 64)
		out = append(out, trade.Position{
			Symbol:        strings.TrimSuffix(strings.ToUpper(r.Symbol), "USDT"),
			Side:          side,
			Quantity:      abs(amt),
			EntryPrice:    entry,
			MarkPrice:     mark,
			UnrealizedPnL: upnl,
			Leverage:      int(levF),
		})
	}
	return out, nil
}

// ── Helpers ──────────────────────────────────────────────────────────────

func qtyString(q float64) string {
	s := strconv.FormatFloat(q, 'f', 6, 64)
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

// silence "imported and not used" if math/big ever drops out of the
// signing path during refactors.
var _ = big.NewInt

var _ trade.Adapter = (*Adapter)(nil)
