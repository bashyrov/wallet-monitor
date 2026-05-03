// Hyperliquid trade adapter — agent-wallet personal_sign auth.
//
// Port of `backend/services/trade_adapters/hyperliquid.py`.
//
// Auth: an "Agent Wallet" — a separate ETH keypair the user creates on
// hyperliquid.xyz and links to their main wallet. The agent can place
// trades but cannot withdraw. We sign the action JSON's SHA-256 with
// `personal_sign` (eth_sign-style; matches Python adapter).
//
// Endpoints
//
//	POST /info     — public reads (clearinghouseState, meta, userFunding)
//	POST /exchange — signed actions (order, updateLeverage, …)
//
// Wire shape
//
//	{
//	  "action": { … original action with "nonce" injected … },
//	  "nonce":  <ms>,
//	  "signature": { "r": "0x…", "s": "0x…", "v": 27|28 },
//	  "vaultAddress": null
//	}
//
// Quirks
//
//   - Asset index lookup hits /info?type=meta and is cached 1h. Index
//     changes only when a new perp is listed.
//   - `s` (size) MUST be a string. Floats stringify mid-place_order.
//   - Reduce-only flag is `r: true` on the order leg.
//   - Market orders are encoded as IoC limits with `p:"0"` — Python
//     does the same. Hyperliquid handles the slippage internally.
package hyperliquid

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"math/big"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/ethereum/go-ethereum/crypto"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

const baseURL = "https://api.hyperliquid.xyz"

type Adapter struct {
	httpClient *http.Client

	assetMu  sync.RWMutex
	assets   map[string]int
	assetsAt time.Time
}

const assetTTL = time.Hour

func New() *Adapter {
	return &Adapter{
		httpClient: &http.Client{
			Timeout: 15 * time.Second,
			Transport: &http.Transport{
				MaxIdleConnsPerHost: 8,
				IdleConnTimeout:     60 * time.Second,
			},
		},
		assets: map[string]int{},
	}
}

func init() { trade.Register("hyperliquid", New()) }

func (a *Adapter) Name() string { return "hyperliquid" }

// ── Signing ──────────────────────────────────────────────────────────────

// signAction returns r/s/v split out for HL's `signature` envelope.
// Matches Python's:
//
//	action_hash = sha256(json(action))         # 32 bytes hex
//	digest      = personal_sign(action_hash)
//	sig         = sign(digest, agent_priv_key)
//
// Note: this is the form used by the Python adapter today. Hyperliquid's
// production signing scheme actually involves a "phantom agent"
// EIP-712 wrap, but the Python adapter (already in production) uses
// the simpler personal_sign and that's what we mirror.
func signAction(action map[string]any, privKeyHex string) (r, s string, v int, err error) {
	canon, err := json.Marshal(action)
	if err != nil {
		return "", "", 0, fmt.Errorf("marshal action: %w", err)
	}
	hashHex := sha256.Sum256(canon)
	hexBytes := hex.EncodeToString(hashHex[:])

	prefix := []byte(fmt.Sprintf("\x19Ethereum Signed Message:\n%d", len(hexBytes)))
	digest := crypto.Keccak256(append(prefix, []byte(hexBytes)...))

	priv, err := crypto.HexToECDSA(strings.TrimPrefix(privKeyHex, "0x"))
	if err != nil {
		return "", "", 0, fmt.Errorf("parse private key: %w", err)
	}
	sig, err := crypto.Sign(digest, priv)
	if err != nil {
		return "", "", 0, fmt.Errorf("sign: %w", err)
	}
	if len(sig) != 65 {
		return "", "", 0, fmt.Errorf("unexpected sig length %d", len(sig))
	}
	rBig := new(big.Int).SetBytes(sig[:32])
	sBig := new(big.Int).SetBytes(sig[32:64])
	vByte := int(sig[64]) + 27
	return "0x" + rBig.Text(16), "0x" + sBig.Text(16), vByte, nil
}

func (a *Adapter) postAction(ctx context.Context, creds trade.Creds, action map[string]any) (json.RawMessage, error) {
	if creds.PrivateKey == "" && creds.APISecret == "" {
		return nil, errUser("hyperliquid requires an agent-wallet private key")
	}
	priv := creds.PrivateKey
	if priv == "" {
		priv = creds.APISecret
	}
	nonce := time.Now().UnixMilli()
	action["nonce"] = nonce

	r, s, v, err := signAction(action, priv)
	if err != nil {
		return nil, errInternal("sign action", err)
	}

	payload := map[string]any{
		"action":       action,
		"nonce":        nonce,
		"signature":    map[string]any{"r": r, "s": s, "v": v},
		"vaultAddress": nil,
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return nil, errInternal("marshal payload", err)
	}
	return a.postJSON(ctx, "/exchange", body)
}

func (a *Adapter) postJSON(ctx context.Context, path string, body []byte) (json.RawMessage, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, baseURL+path, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
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
	if len(raw) == 0 {
		return json.RawMessage("{}"), nil
	}
	// HL packs business errors inside a 200 response: {"status":"err",...}
	var env struct {
		Status   string          `json:"status"`
		Response json.RawMessage `json:"response"`
	}
	if err := json.Unmarshal(raw, &env); err == nil && env.Status == "err" {
		return nil, &trade.Error{Kind: trade.KindExchange, Message: strings.TrimSpace(string(env.Response))}
	}
	return raw, nil
}

func (a *Adapter) postInfo(ctx context.Context, body map[string]any) (json.RawMessage, error) {
	b, err := json.Marshal(body)
	if err != nil {
		return nil, errInternal("marshal info", err)
	}
	return a.postJSON(ctx, "/info", b)
}

func parseError(status int, body []byte) *trade.Error {
	msg := strings.TrimSpace(string(body))
	if status == 429 {
		return &trade.Error{Kind: trade.KindRateLimit, Message: msg}
	}
	return &trade.Error{Kind: trade.KindExchange, Message: msg}
}

// ── Asset-index cache ────────────────────────────────────────────────────

func (a *Adapter) assetIndex(ctx context.Context, symbol string) (int, error) {
	sym := strings.ToUpper(symbol)
	a.assetMu.RLock()
	if time.Since(a.assetsAt) < assetTTL {
		if idx, ok := a.assets[sym]; ok {
			a.assetMu.RUnlock()
			return idx, nil
		}
	}
	a.assetMu.RUnlock()

	body, err := a.postInfo(ctx, map[string]any{"type": "meta"})
	if err != nil {
		return 0, err
	}
	var resp struct {
		Universe []struct {
			Name string `json:"name"`
		} `json:"universe"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		return 0, errInternal("parse universe", err)
	}
	a.assetMu.Lock()
	a.assets = make(map[string]int, len(resp.Universe))
	for i, u := range resp.Universe {
		if u.Name != "" {
			a.assets[strings.ToUpper(u.Name)] = i
		}
	}
	a.assetsAt = time.Now()
	idx, ok := a.assets[sym]
	a.assetMu.Unlock()
	if !ok {
		return 0, errUser("%s is not listed on hyperliquid", sym)
	}
	return idx, nil
}

// ── Adapter methods ──────────────────────────────────────────────────────

func (a *Adapter) GetBalance(ctx context.Context, creds trade.Creds) (*trade.Balance, error) {
	address := creds.APIKey
	if address == "" {
		address = creds.Wallet
	}
	if address == "" {
		return nil, errUser("hyperliquid requires the main wallet address")
	}
	body, err := a.postInfo(ctx, map[string]any{"type": "clearinghouseState", "user": address})
	if err != nil {
		return nil, err
	}
	var resp struct {
		MarginSummary struct {
			AccountValue json.Number `json:"accountValue"`
		} `json:"marginSummary"`
		Withdrawable json.Number `json:"withdrawable"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		return nil, errInternal("parse balance", err)
	}
	total, _ := resp.MarginSummary.AccountValue.Float64()
	avail, _ := resp.Withdrawable.Float64()
	if avail == 0 {
		avail = total
	}
	return &trade.Balance{TotalUSD: total, AvailableUSD: avail}, nil
}

func (a *Adapter) SetLeverage(ctx context.Context, creds trade.Creds, req trade.LeverageRequest) error {
	idx, err := a.assetIndex(ctx, req.Symbol)
	if err != nil {
		return err
	}
	action := map[string]any{
		"type":     "updateLeverage",
		"asset":    idx,
		"isCross":  req.MarginMode == trade.MarginCross,
		"leverage": req.Leverage,
	}
	_, err = a.postAction(ctx, creds, action)
	return err
}

func (a *Adapter) PlaceOrder(ctx context.Context, creds trade.Creds, req trade.OpenRequest) (*trade.Result, error) {
	if err := req.Validate(); err != nil {
		return nil, err
	}
	idx, err := a.assetIndex(ctx, req.Symbol)
	if err != nil {
		return nil, err
	}
	action := map[string]any{
		"type": "order",
		"orders": []map[string]any{{
			"a": idx,
			"b": req.Side == trade.SideBuy,
			"p": "0",
			"s": qtyString(req.Quantity),
			"r": false,
			"t": map[string]any{"limit": map[string]any{"tif": "Ioc"}},
		}},
		"grouping": "na",
	}
	body, err := a.postAction(ctx, creds, action)
	if err != nil {
		return nil, err
	}
	oid := extractOrderID(body)
	return &trade.Result{
		OrderID:   oid,
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
	idx, err := a.assetIndex(ctx, req.Symbol)
	if err != nil {
		return nil, err
	}
	closeIsBuy := p.Side == trade.SideSell
	action := map[string]any{
		"type": "order",
		"orders": []map[string]any{{
			"a": idx,
			"b": closeIsBuy,
			"p": "0",
			"s": qtyString(p.Quantity),
			"r": true,
			"t": map[string]any{"limit": map[string]any{"tif": "Ioc"}},
		}},
		"grouping": "na",
	}
	body, err := a.postAction(ctx, creds, action)
	if err != nil {
		return nil, err
	}
	oid := extractOrderID(body)
	closeSide := trade.SideSell
	if closeIsBuy {
		closeSide = trade.SideBuy
	}
	return &trade.Result{
		OrderID:   oid,
		Symbol:    req.Symbol,
		Side:      closeSide,
		Quantity:  p.Quantity,
		Status:    "NEW",
		CreatedAt: time.Now().UTC(),
		Raw:       body,
	}, nil
}

// extractOrderID picks the oid out of HL's response envelope:
//
//	{"status":"ok","response":{"data":{"statuses":[{"resting":{"oid":...}}|{"filled":{"oid":...}}]}}}
func extractOrderID(body []byte) string {
	var env struct {
		Response struct {
			Data struct {
				Statuses []map[string]json.RawMessage `json:"statuses"`
			} `json:"data"`
		} `json:"response"`
	}
	if err := json.Unmarshal(body, &env); err != nil {
		return ""
	}
	if len(env.Response.Data.Statuses) == 0 {
		return ""
	}
	for _, key := range []string{"resting", "filled"} {
		raw, ok := env.Response.Data.Statuses[0][key]
		if !ok {
			continue
		}
		var inner struct {
			Oid json.Number `json:"oid"`
		}
		if err := json.Unmarshal(raw, &inner); err == nil {
			return inner.Oid.String()
		}
	}
	return ""
}

func (a *Adapter) ListPositions(ctx context.Context, creds trade.Creds, symbol string) ([]trade.Position, error) {
	address := creds.APIKey
	if address == "" {
		address = creds.Wallet
	}
	if address == "" {
		return nil, errUser("hyperliquid requires the main wallet address")
	}
	body, err := a.postInfo(ctx, map[string]any{"type": "clearinghouseState", "user": address})
	if err != nil {
		return nil, err
	}
	var resp struct {
		AssetPositions []struct {
			Position struct {
				Coin           string      `json:"coin"`
				Szi            json.Number `json:"szi"`
				EntryPx        json.Number `json:"entryPx"`
				PositionValue  json.Number `json:"positionValue"`
				UnrealizedPnl  json.Number `json:"unrealizedPnl"`
				Leverage       struct {
					Value json.Number `json:"value"`
				} `json:"leverage"`
			} `json:"position"`
		} `json:"assetPositions"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		return nil, errInternal("parse positions", err)
	}
	want := strings.ToUpper(symbol)
	out := make([]trade.Position, 0, len(resp.AssetPositions))
	for _, p := range resp.AssetPositions {
		sz, _ := p.Position.Szi.Float64()
		if sz == 0 {
			continue
		}
		coin := p.Position.Coin
		if want != "" && strings.ToUpper(coin) != want {
			continue
		}
		side := trade.SideBuy
		if sz < 0 {
			side = trade.SideSell
		}
		entry, _ := p.Position.EntryPx.Float64()
		posVal, _ := p.Position.PositionValue.Float64()
		mark := 0.0
		if sz != 0 {
			mark = posVal / abs(sz)
		}
		upnl, _ := p.Position.UnrealizedPnl.Float64()
		levF, _ := p.Position.Leverage.Value.Float64()
		out = append(out, trade.Position{
			Symbol:        coin,
			Side:          side,
			Quantity:      abs(sz),
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
