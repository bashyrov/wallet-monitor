// Paradex trade adapter — Stark-key signing on the Paraclear chain.
//
// Direct port of https://github.com/tradeparadex/paradex-py — auth, JWT
// caching, order signing — but written entirely in Go so we don't have
// to drag paradex-py + starknet-py through Python 3.13 (where the SDK
// pin is currently broken).
//
// Credentials map (matches the existing wallet schema):
//
//	APIKey     → L2 account address (0x… felt) — main account
//	APISecret  → Stark private key (felt252, hex with or without 0x)
//	             — either main private key OR subkey private key
//	Passphrase → (optional) subkey public key (0x…). If set, auth hits
//	             /v1/auth/{pubkey}; the signature is from the subkey
//	             but PARADEX-STARKNET-ACCOUNT stays as the main address.
//	             Recommended — subkeys cannot withdraw/transfer funds.
//
// Signing
//
//	auth   = SNIP-12 typed data — Request{ method, path, body, timestamp,
//	         expiration } over StarkNet domain { Paradex, chainId, "1" }
//	         where chainId = int.from_bytes(b"PRIVATE_SN_PARACLEAR_MAINNET",
//	         "big") rendered hex.
//	order  = SNIP-12 typed data — Order{ timestamp, market, side,
//	         orderType, size, price } where size/price are signed
//	         felt-encoded with 8-decimal scaling (Paradex's quantum).
//
// JWT TTL: Paradex JWTs expire every 5 minutes (hard-coded server-side).
// We refresh ~90s before expiry so callers always have a fresh token.
//
// Wire shape — orders post to /v1/orders with the usual JSON body plus
// `signature` (decimal `["r","s"]`) and `signature_timestamp` (ms).
package paradex

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math/big"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/NethermindEth/juno/core/felt"
	"github.com/NethermindEth/starknet.go/curve"
	"github.com/NethermindEth/starknet.go/typeddata"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

const (
	baseURL          = "https://api.prod.paradex.trade"
	starknetChainID  = "PRIVATE_SN_PARACLEAR_MAINNET"
	// Paradex's auth endpoint hard-caps JWTs at 5 min. We ask for 4 min
	// to stay comfortably under, and refresh 90 s early so a Stark sign
	// + network round-trip never races a request to /v1/orders.
	jwtTTL           = 4 * time.Minute
	jwtRefreshLeeway = 90 * time.Second
)

// chainIDHex = hex(int.from_bytes(starknetChainID.encode(), "big")).
// Computed once at init. This is the value Paradex puts in the
// SNIP-12 domain.
var chainIDHex = "0x" + new(big.Int).SetBytes([]byte(starknetChainID)).Text(16)

type Adapter struct {
	httpClient *http.Client

	jwtMu sync.Mutex
	jwts  map[string]jwtEntry // keyed by L2 address (lowercased)
}

type jwtEntry struct {
	token   string
	expires time.Time
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
		jwts: map[string]jwtEntry{},
	}
}

func init() { trade.Register("paradex", New()) }

func (a *Adapter) Name() string { return "paradex" }

// ── Stark signing helpers ────────────────────────────────────────────────

// signTypedData computes the SNIP-12 message hash for `tdJSON` against
// `address` and signs the hash with `privKeyHex`. Returns (r, s) as
// 0x-prefixed hex AND decimal — Paradex uses decimal in its
// PARADEX-STARKNET-SIGNATURE header.
func signTypedData(tdJSON []byte, address, privKeyHex string) (rDec, sDec string, err error) {
	var td typeddata.TypedData
	if err := json.Unmarshal(tdJSON, &td); err != nil {
		return "", "", fmt.Errorf("typed data: %w", err)
	}
	hash, err := td.GetMessageHash(address)
	if err != nil {
		return "", "", fmt.Errorf("get message hash: %w", err)
	}

	priv, ok := new(big.Int).SetString(strings.TrimPrefix(privKeyHex, "0x"), 16)
	if !ok {
		return "", "", fmt.Errorf("parse private key")
	}
	privFelt := new(felt.Felt).SetBigInt(priv)
	r, s, err := curve.SignFelts(hash, privFelt)
	if err != nil {
		return "", "", fmt.Errorf("stark sign: %w", err)
	}
	return r.BigInt(new(big.Int)).String(), s.BigInt(new(big.Int)).String(), nil
}

// flattenSignature mirrors paradex-py's flatten_signature:
// `f'["{r}","{s}"]'` with r and s as decimal strings.
func flattenSignature(rDec, sDec string) string {
	return `["` + rDec + `","` + sDec + `"]`
}

// ── Typed-data builders ──────────────────────────────────────────────────

// buildAuthMessage returns the SNIP-12 typed data for /v1/auth. The
// signed `path` field in the message MUST match the URL path used —
// "/v1/auth" for main-key auth, "/v1/auth/{pubkey}" for subkeys.
func buildAuthMessage(timestamp, expiration int64, path string) []byte {
	td := map[string]any{
		"domain": map[string]any{
			"name":    "Paradex",
			"chainId": chainIDHex,
			"version": "1",
		},
		"primaryType": "Request",
		"types": map[string]any{
			"StarkNetDomain": []map[string]string{
				{"name": "name", "type": "felt"},
				{"name": "chainId", "type": "felt"},
				{"name": "version", "type": "felt"},
			},
			"Request": []map[string]string{
				{"name": "method", "type": "felt"},
				{"name": "path", "type": "felt"},
				{"name": "body", "type": "felt"},
				{"name": "timestamp", "type": "felt"},
				{"name": "expiration", "type": "felt"},
			},
		},
		"message": map[string]any{
			"method": "POST",
			"path":   path,
			// Paradex sends `body: ""` here — starknet-py encodes empty
			// string as felt 0. starknet.go's StrToHex chokes on "", so
			// we pass "0" which lands on the exact same felt.
			"body":       "0",
			"timestamp":  strconv.FormatInt(timestamp, 10),
			"expiration": strconv.FormatInt(expiration, 10),
		},
	}
	b, _ := json.Marshal(td)
	return b
}

// buildOrderMessage matches paradex-py's build_order_message. size and
// price are felt-encoded with 8-decimal Paradex quantum (e.g. 0.001
// BTC → "100000"). chainSide is "1" for buy, "2" for sell. orderType
// is the literal "MARKET" / "LIMIT" string.
func buildOrderMessage(signatureTimestampMs int64, market, chainSide, orderType, chainSize, chainPrice string) []byte {
	td := map[string]any{
		"domain": map[string]any{
			"name":    "Paradex",
			"chainId": chainIDHex,
			"version": "1",
		},
		"primaryType": "Order",
		"types": map[string]any{
			"StarkNetDomain": []map[string]string{
				{"name": "name", "type": "felt"},
				{"name": "chainId", "type": "felt"},
				{"name": "version", "type": "felt"},
			},
			"Order": []map[string]string{
				{"name": "timestamp", "type": "felt"},
				{"name": "market", "type": "felt"},
				{"name": "side", "type": "felt"},
				{"name": "orderType", "type": "felt"},
				{"name": "size", "type": "felt"},
				{"name": "price", "type": "felt"},
			},
		},
		"message": map[string]any{
			"timestamp": strconv.FormatInt(signatureTimestampMs, 10),
			"market":    market,
			"side":      chainSide,
			"orderType": orderType,
			"size":      chainSize,
			"price":     chainPrice,
		},
	}
	b, _ := json.Marshal(td)
	return b
}

// ── JWT auth ─────────────────────────────────────────────────────────────

func (a *Adapter) ensureJWT(ctx context.Context, creds trade.Creds) (string, error) {
	if creds.APIKey == "" || creds.APISecret == "" {
		return "", errUser("paradex requires L2 address (api_key) and Stark private key (api_secret)")
	}
	// Subkey path: /v1/auth/{public_key}. The signed `path` field in
	// the SNIP-12 message MUST match exactly. Header still names the
	// main account address; verifier knows to load the subkey from the
	// path and verify against that pubkey.
	subkeyPub := strings.TrimSpace(creds.Passphrase)
	authPath := "/v1/auth"
	if subkeyPub != "" {
		authPath = "/v1/auth/" + subkeyPub
	}

	// Cache key: address+subkey so a user with both main+subkey on the
	// same wallet doesn't share a token across them.
	cacheKey := strings.ToLower(creds.APIKey) + "|" + strings.ToLower(subkeyPub)
	a.jwtMu.Lock()
	defer a.jwtMu.Unlock()

	if e, ok := a.jwts[cacheKey]; ok && time.Until(e.expires) > jwtRefreshLeeway {
		return e.token, nil
	}

	timestamp := time.Now().Unix()
	expiry := timestamp + int64(jwtTTL.Seconds())

	tdJSON := buildAuthMessage(timestamp, expiry, authPath)
	rDec, sDec, err := signTypedData(tdJSON, creds.APIKey, creds.APISecret)
	if err != nil {
		return "", errInternal("auth sign", err)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, baseURL+authPath, nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("PARADEX-STARKNET-ACCOUNT", creds.APIKey)
	req.Header.Set("PARADEX-STARKNET-SIGNATURE", flattenSignature(rDec, sDec))
	req.Header.Set("PARADEX-TIMESTAMP", strconv.FormatInt(timestamp, 10))
	req.Header.Set("PARADEX-SIGNATURE-EXPIRATION", strconv.FormatInt(expiry, 10))
	req.Header.Set("Accept", "application/json")

	resp, err := a.httpClient.Do(req)
	if err != nil {
		return "", &trade.Error{Kind: trade.KindTransient, Message: err.Error(), Cause: err}
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return "", parseError(resp.StatusCode, body)
	}
	var jwtResp struct {
		JWT string `json:"jwt_token"`
	}
	if err := json.Unmarshal(body, &jwtResp); err != nil || jwtResp.JWT == "" {
		return "", &trade.Error{Kind: trade.KindExchange, Message: "paradex: missing jwt_token in /v1/auth response"}
	}
	a.jwts[cacheKey] = jwtEntry{token: jwtResp.JWT, expires: time.Unix(expiry, 0)}
	return jwtResp.JWT, nil
}

// ── HTTP plumbing ────────────────────────────────────────────────────────

func (a *Adapter) authedRequest(ctx context.Context, creds trade.Creds, method, path string, body []byte) (json.RawMessage, error) {
	jwt, err := a.ensureJWT(ctx, creds)
	if err != nil {
		return nil, err
	}
	var br io.Reader
	if body != nil {
		br = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, baseURL+path, br)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+jwt)
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, &trade.Error{Kind: trade.KindTransient, Message: err.Error(), Cause: err}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	if resp.StatusCode == 401 {
		// JWT expired or invalidated — drop cache so caller can retry.
		// Cache key matches ensureJWT: address|subkey-pubkey.
		cacheKey := strings.ToLower(creds.APIKey) + "|" + strings.ToLower(strings.TrimSpace(creds.Passphrase))
		a.jwtMu.Lock()
		delete(a.jwts, cacheKey)
		a.jwtMu.Unlock()
		return nil, parseError(resp.StatusCode, raw)
	}
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
	if status == 401 || status == 403 {
		return &trade.Error{Kind: trade.KindUser, Message: msg}
	}
	return &trade.Error{Kind: trade.KindExchange, Message: msg}
}

// ── Adapter methods ──────────────────────────────────────────────────────

func (a *Adapter) GetBalance(ctx context.Context, creds trade.Creds) (*trade.Balance, error) {
	body, err := a.authedRequest(ctx, creds, http.MethodGet, "/v1/balance", nil)
	if err != nil {
		return nil, err
	}
	var resp struct {
		Results []struct {
			Token string      `json:"token"`
			Size  json.Number `json:"size"`
		} `json:"results"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		return nil, errInternal("parse balance", err)
	}
	var total float64
	for _, r := range resp.Results {
		if strings.ToUpper(r.Token) != "USDC" {
			continue
		}
		v, _ := r.Size.Float64()
		total += v
	}
	return &trade.Balance{TotalUSD: total, AvailableUSD: total}, nil
}

func (a *Adapter) ListPositions(ctx context.Context, creds trade.Creds, symbol string) ([]trade.Position, error) {
	body, err := a.authedRequest(ctx, creds, http.MethodGet, "/v1/positions", nil)
	if err != nil {
		return nil, err
	}
	// Field names per Paradex's published OpenAPI (responses.PositionResp).
	// `mark_price` is NOT in this payload — we derive it below from
	// (entry, unrealized_pnl, size). `cost_usd` is the position notional;
	// `unrealized_funding_pnl` is the running funding accrual we surface
	// as funding_pnl_usd to the rest of Avalant.
	var resp struct {
		Results []struct {
			Market               string      `json:"market"`
			Size                 json.Number `json:"size"`
			Side                 string      `json:"side"`
			AverageEntry         json.Number `json:"average_entry_price"`
			AverageEntryUSD      json.Number `json:"average_entry_price_usd"`
			CostUSD              json.Number `json:"cost_usd"`
			UnrealizedPnL        json.Number `json:"unrealized_pnl"`
			UnrealizedFundingPnL json.Number `json:"unrealized_funding_pnl"`
			Leverage             json.Number `json:"leverage"`
			Status               string      `json:"status"`
			CreatedAt            int64       `json:"created_at"`
		} `json:"results"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		return nil, errInternal("parse positions", err)
	}
	want := strings.ToUpper(symbol)
	out := make([]trade.Position, 0, len(resp.Results))
	for _, p := range resp.Results {
		// Skip closed positions — `size` would be 0 too but check status
		// explicitly so a partial payload doesn't surface as ghost rows.
		if strings.EqualFold(p.Status, "CLOSED") {
			continue
		}
		sz, _ := p.Size.Float64()
		if sz == 0 {
			continue
		}
		coin := strings.SplitN(p.Market, "-", 2)[0] // "BTC-USD-PERP" → "BTC"
		if want != "" && strings.ToUpper(coin) != want {
			continue
		}
		side := trade.SideBuy
		if strings.EqualFold(p.Side, "SHORT") {
			side = trade.SideSell
		}
		entry, _ := p.AverageEntry.Float64()
		upnl, _ := p.UnrealizedPnL.Float64()
		funding, _ := p.UnrealizedFundingPnL.Float64()
		costUSD, _ := p.CostUSD.Float64()
		levF, _ := p.Leverage.Float64()
		quantity := abs(sz)

		// Mark price isn't on /v1/positions — invert from PnL identity:
		//   long  PnL = (mark - entry) * qty   →  mark = entry + PnL/qty
		//   short PnL = (entry - mark) * qty   →  mark = entry - PnL/qty
		// Note: unrealized_pnl already includes funding per the spec, so we
		// subtract it back out to get the pure price-based component first.
		mark := 0.0
		if quantity > 0 && entry > 0 {
			pricePnL := upnl - funding
			if side == trade.SideBuy {
				mark = entry + pricePnL/quantity
			} else {
				mark = entry - pricePnL/quantity
			}
			if mark < 0 {
				mark = 0
			}
		}

		// Notional: prefer Paradex's cost_usd (as reported), fall back
		// to qty × mark for sanity if the API omits it.
		notional := costUSD
		if notional <= 0 && mark > 0 {
			notional = quantity * mark
		}

		var openedAt time.Time
		if p.CreatedAt > 0 {
			openedAt = time.Unix(p.CreatedAt/1000, (p.CreatedAt%1000)*1_000_000).UTC()
		}

		out = append(out, trade.Position{
			Symbol:        coin,
			Side:          side,
			Quantity:      quantity,
			EntryPrice:    entry,
			MarkPrice:     mark,
			Notional:      notional,
			UnrealizedPnL: upnl,
			FundingPnL:    funding,
			Leverage:      int(levF),
			OpenedAt:      openedAt,
		})
	}
	return out, nil
}

// SetLeverage is per-market on Paradex but is a separate auth-protected
// endpoint. We call it through the JWT path. No Stark signature needed
// for this endpoint.
func (a *Adapter) SetLeverage(ctx context.Context, creds trade.Creds, req trade.LeverageRequest) error {
	if req.Symbol == "" {
		return errUser("symbol required")
	}
	if req.Leverage <= 0 {
		return errUser("leverage must be > 0")
	}
	market := toParadexMarket(req.Symbol)
	body, _ := json.Marshal(map[string]any{
		"market":   market,
		"leverage": req.Leverage,
	})
	_, err := a.authedRequest(ctx, creds, http.MethodPost, "/v1/account/leverage", body)
	return err
}

func (a *Adapter) PlaceOrder(ctx context.Context, creds trade.Creds, req trade.OpenRequest) (*trade.Result, error) {
	if err := req.Validate(); err != nil {
		return nil, err
	}
	market := toParadexMarket(req.Symbol)
	now := time.Now().UnixMilli()

	chainSide := "1"
	if req.Side == trade.SideSell {
		chainSide = "2"
	}
	chainSize := chainQuantum(req.Quantity)
	chainPrice := "0" // market order

	tdJSON := buildOrderMessage(now, market, chainSide, "MARKET", chainSize, chainPrice)
	rDec, sDec, err := signTypedData(tdJSON, creds.APIKey, creds.APISecret)
	if err != nil {
		return nil, errInternal("order sign", err)
	}

	body, _ := json.Marshal(map[string]any{
		"market":              market,
		"side":                strings.ToUpper(string(req.Side)),
		"size":                qtyString(req.Quantity),
		"type":                "MARKET",
		"client_id":           "",
		"instruction":         "GTC",
		"signature":           flattenSignature(rDec, sDec),
		"signature_timestamp": now,
	})
	resp, err := a.authedRequest(ctx, creds, http.MethodPost, "/v1/orders", body)
	if err != nil {
		return nil, err
	}
	id := extractOrderID(resp)
	return &trade.Result{
		OrderID:   id,
		Symbol:    req.Symbol,
		Side:      req.Side,
		Quantity:  req.Quantity,
		Status:    "NEW",
		CreatedAt: time.Now().UTC(),
		Raw:       resp,
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
	market := toParadexMarket(req.Symbol)
	now := time.Now().UnixMilli()

	closeSide := trade.SideSell
	chainSide := "2"
	if p.Side == trade.SideSell {
		closeSide = trade.SideBuy
		chainSide = "1"
	}
	chainSize := chainQuantum(p.Quantity)

	tdJSON := buildOrderMessage(now, market, chainSide, "MARKET", chainSize, "0")
	rDec, sDec, err := signTypedData(tdJSON, creds.APIKey, creds.APISecret)
	if err != nil {
		return nil, errInternal("order sign", err)
	}

	body, _ := json.Marshal(map[string]any{
		"market":              market,
		"side":                strings.ToUpper(string(closeSide)),
		"size":                qtyString(p.Quantity),
		"type":                "MARKET",
		"client_id":           "",
		"instruction":         "GTC",
		"flags":               []string{"REDUCE_ONLY"},
		"signature":           flattenSignature(rDec, sDec),
		"signature_timestamp": now,
	})
	resp, err := a.authedRequest(ctx, creds, http.MethodPost, "/v1/orders", body)
	if err != nil {
		return nil, err
	}
	id := extractOrderID(resp)
	return &trade.Result{
		OrderID:   id,
		Symbol:    req.Symbol,
		Side:      closeSide,
		Quantity:  p.Quantity,
		Status:    "NEW",
		CreatedAt: time.Now().UTC(),
		Raw:       resp,
	}, nil
}

// ── Helpers ──────────────────────────────────────────────────────────────

func toParadexMarket(sym string) string {
	return strings.ToUpper(sym) + "-USD-PERP"
}

// chainQuantum converts a float quantity to Paradex's 8-decimal felt
// scale: 0.001 → "100000", 1.5 → "150000000".
func chainQuantum(q float64) string {
	scaled := new(big.Float).Mul(big.NewFloat(q), big.NewFloat(1e8))
	intVal, _ := scaled.Int(nil)
	return intVal.String()
}

func qtyString(q float64) string {
	s := strconv.FormatFloat(q, 'f', 8, 64)
	if strings.Contains(s, ".") {
		s = strings.TrimRight(strings.TrimRight(s, "0"), ".")
	}
	if s == "" {
		s = "0"
	}
	return s
}

func extractOrderID(body []byte) string {
	var resp struct {
		ID json.RawMessage `json:"id"`
	}
	if err := json.Unmarshal(body, &resp); err == nil && len(resp.ID) > 0 {
		s := strings.TrimSpace(string(resp.ID))
		if strings.HasPrefix(s, `"`) && strings.HasSuffix(s, `"`) && len(s) >= 2 {
			return s[1 : len(s)-1]
		}
		if s != "null" {
			return s
		}
	}
	return ""
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
