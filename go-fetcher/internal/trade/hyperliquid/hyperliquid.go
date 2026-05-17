// Hyperliquid trade adapter — agent-wallet phantom-agent EIP-712.
//
// Port of `backend/services/trade_adapters/hyperliquid.py` using the
// same signing scheme the official `hyperliquid-py` SDK ships with.
// (The earlier `personal_sign(sha256(...))` form in this file was
// wrong — HL's exchange endpoint rejects it on real orders.)
//
// Auth: an "Agent Wallet" — a separate ETH keypair the user creates on
// hyperliquid.xyz/agentWallet and links to their main wallet. The
// agent can place trades but cannot withdraw.
//
// Signing
//
//	packed       = msgpack(action) || nonce_be8 || vault_marker
//	vault_marker = 0x00                         if no vault
//	             | 0x01 || bytes20(vaultAddr)   otherwise
//	connectionId = keccak256(packed)            (32 bytes)
//	domain       = { name: "Exchange", version: "1",
//	                 chainId: 1337, verifyingContract: 0x0…0 }
//	type         = Agent(string source, bytes32 connectionId)
//	message      = { source: "a" (mainnet) | "b" (testnet),
//	                 connectionId }
//	sig          = EIP-712 sign(domain, Agent, message) by agent key
//
// Wire payload
//
//	{ "action":..., "nonce":<ms>, "signature":{r,s,v},
//	  "vaultAddress": null }
//
// Quirks
//
//   - Asset index lookup hits /info?type=meta and is cached 1h. Index
//     changes only when a new perp is listed.
//   - `s` (size) and `p` (price) MUST be strings.
//   - Reduce-only flag is `r:true` on the order leg.
//   - Market orders are encoded as IoC limits with `p:"0"` — HL handles
//     the slippage internally.
//   - msgpack of the action must agree byte-for-byte with what HL
//     re-packs server-side. We use struct field-declaration order
//     (matching the SDK's dict-insertion order), so vmihailenco/msgpack
//     produces the same bytes as msgpack-python given the same fields.
package hyperliquid

import (
	"bytes"
	"context"
	"encoding/binary"
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

	gethmath "github.com/ethereum/go-ethereum/common/math"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/signer/core/apitypes"
	"github.com/vmihailenco/msgpack/v5"

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
				ForceAttemptHTTP2:   true,
				MaxIdleConns:        200,
				MaxIdleConnsPerHost: 32,
				MaxConnsPerHost:     64,
				IdleConnTimeout:     300 * time.Second,
				TLSHandshakeTimeout: 5 * time.Second,
			},
		},
		assets: map[string]int{},
	}
}

func init() { trade.Register("hyperliquid", New()) }

func (a *Adapter) Name() string { return "hyperliquid" }

// ── Action types ─────────────────────────────────────────────────────────
//
// Field declaration order is the wire / msgpack order. Do NOT reorder
// without re-running cross-language signing tests against the Python
// SDK, or HL will reject every order with a signature mismatch.

type orderLimit struct {
	Tif string `msgpack:"tif" json:"tif"`
}

type orderTypeBox struct {
	Limit orderLimit `msgpack:"limit" json:"limit"`
}

type orderLeg struct {
	A int          `msgpack:"a" json:"a"`
	B bool         `msgpack:"b" json:"b"`
	P string       `msgpack:"p" json:"p"`
	S string       `msgpack:"s" json:"s"`
	R bool         `msgpack:"r" json:"r"`
	T orderTypeBox `msgpack:"t" json:"t"`
}

type orderAction struct {
	Type     string     `msgpack:"type" json:"type"`
	Orders   []orderLeg `msgpack:"orders" json:"orders"`
	Grouping string     `msgpack:"grouping" json:"grouping"`
}

// Trigger order types (stop/TP). Separate structs to avoid altering the
// field layout of the regular order structs (msgpack field order is wire order).
type triggerType struct {
	IsMarket  bool   `msgpack:"isMarket" json:"isMarket"`
	TriggerPx string `msgpack:"triggerPx" json:"triggerPx"`
	Tpsl      string `msgpack:"tpsl" json:"tpsl"`
}

type triggerTypeBox struct {
	Trigger triggerType `msgpack:"trigger" json:"trigger"`
}

type triggerOrderLeg struct {
	A int            `msgpack:"a" json:"a"`
	B bool           `msgpack:"b" json:"b"`
	P string         `msgpack:"p" json:"p"`
	S string         `msgpack:"s" json:"s"`
	R bool           `msgpack:"r" json:"r"`
	T triggerTypeBox `msgpack:"t" json:"t"`
}

type triggerOrderAction struct {
	Type     string            `msgpack:"type" json:"type"`
	Orders   []triggerOrderLeg `msgpack:"orders" json:"orders"`
	Grouping string            `msgpack:"grouping" json:"grouping"`
}

type updateLeverageAction struct {
	Type     string `msgpack:"type" json:"type"`
	Asset    int    `msgpack:"asset" json:"asset"`
	IsCross  bool   `msgpack:"isCross" json:"isCross"`
	Leverage int    `msgpack:"leverage" json:"leverage"`
}

// ── Signing ──────────────────────────────────────────────────────────────

// signPhantomAgent signs the msgpack-packed action using the HL
// phantom-agent EIP-712 wrap. Returns the {r,s,v} triple in the form
// HL expects (`v` ∈ {27,28}, `r`/`s` 0x-prefixed lower-hex).
func signPhantomAgent(packed []byte, nonce int64, vaultAddress string, isMainnet bool, privKeyHex string) (string, string, int, error) {
	var buf bytes.Buffer
	buf.Write(packed)
	var nonceBuf [8]byte
	binary.BigEndian.PutUint64(nonceBuf[:], uint64(nonce))
	buf.Write(nonceBuf[:])
	if vaultAddress == "" {
		buf.WriteByte(0x00)
	} else {
		buf.WriteByte(0x01)
		addrBytes, err := hex.DecodeString(strings.TrimPrefix(vaultAddress, "0x"))
		if err != nil {
			return "", "", 0, fmt.Errorf("decode vault: %w", err)
		}
		if len(addrBytes) != 20 {
			return "", "", 0, fmt.Errorf("vault address must be 20 bytes, got %d", len(addrBytes))
		}
		buf.Write(addrBytes)
	}
	connectionID := crypto.Keccak256(buf.Bytes())

	source := "a"
	if !isMainnet {
		source = "b"
	}

	td := apitypes.TypedData{
		Types: apitypes.Types{
			"EIP712Domain": []apitypes.Type{
				{Name: "name", Type: "string"},
				{Name: "version", Type: "string"},
				{Name: "chainId", Type: "uint256"},
				{Name: "verifyingContract", Type: "address"},
			},
			"Agent": []apitypes.Type{
				{Name: "source", Type: "string"},
				{Name: "connectionId", Type: "bytes32"},
			},
		},
		PrimaryType: "Agent",
		Domain: apitypes.TypedDataDomain{
			Name:              "Exchange",
			Version:           "1",
			ChainId:           gethmath.NewHexOrDecimal256(1337),
			VerifyingContract: "0x0000000000000000000000000000000000000000",
		},
		Message: apitypes.TypedDataMessage{
			"source":       source,
			"connectionId": connectionID,
		},
	}
	domainSep, err := td.HashStruct("EIP712Domain", td.Domain.Map())
	if err != nil {
		return "", "", 0, fmt.Errorf("eip712 domain: %w", err)
	}
	msgHash, err := td.HashStruct("Agent", td.Message)
	if err != nil {
		return "", "", 0, fmt.Errorf("eip712 agent: %w", err)
	}
	raw := append([]byte{0x19, 0x01}, domainSep...)
	raw = append(raw, msgHash...)
	digest := crypto.Keccak256(raw)

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
	return "0x" + rBig.Text(16), "0x" + sBig.Text(16), int(sig[64]) + 27, nil
}

// packAction msgpack-encodes the action with HL's expected field order.
// Wraps msgpack.Marshal so the call site doesn't need to import it.
func packAction(action any) ([]byte, error) {
	var buf bytes.Buffer
	enc := msgpack.NewEncoder(&buf)
	enc.SetCustomStructTag("msgpack")
	if err := enc.Encode(action); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

func (a *Adapter) postAction(ctx context.Context, creds trade.Creds, action any) (json.RawMessage, error) {
	priv := creds.PrivateKey
	if priv == "" {
		priv = creds.APISecret
	}
	if priv == "" {
		return nil, errUser("hyperliquid requires an agent-wallet private key")
	}
	packed, err := packAction(action)
	if err != nil {
		return nil, errInternal("msgpack action", err)
	}
	nonce := time.Now().UnixMilli()
	r, s, v, err := signPhantomAgent(packed, nonce, "", true, priv)
	if err != nil {
		return nil, errInternal("sign action", err)
	}
	payload := struct {
		Action       any            `json:"action"`
		Nonce        int64          `json:"nonce"`
		Signature    map[string]any `json:"signature"`
		VaultAddress *string        `json:"vaultAddress"`
	}{
		Action:       action,
		Nonce:        nonce,
		Signature:    map[string]any{"r": r, "s": s, "v": v},
		VaultAddress: nil,
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
	action := updateLeverageAction{
		Type:     "updateLeverage",
		Asset:    idx,
		IsCross:  req.MarginMode == trade.MarginCross,
		Leverage: req.Leverage,
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

	if req.OrderType.IsConditional() {
		tpsl := "sl"
		if req.OrderType == trade.OrderTakeProfitMkt {
			tpsl = "tp"
		}
		action := triggerOrderAction{
			Type: "order",
			Orders: []triggerOrderLeg{{
				A: idx,
				B: req.Side == trade.SideBuy,
				P: strconv.FormatFloat(req.StopPrice, 'f', -1, 64),
				S: qtyString(req.Quantity),
				R: false,
				T: triggerTypeBox{Trigger: triggerType{
					IsMarket:  true,
					TriggerPx: strconv.FormatFloat(req.StopPrice, 'f', -1, 64),
					Tpsl:      tpsl,
				}},
			}},
			Grouping: "na",
		}
		body, err := a.postAction(ctx, creds, action)
		if err != nil {
			return nil, err
		}
		oid, _, perr := extractOrderResult(body)
		if perr != nil {
			return nil, perr
		}
		return &trade.Result{
			OrderID:   oid,
			Symbol:    req.Symbol,
			Side:      req.Side,
			Quantity:  req.Quantity,
			Status:    "PENDING",
			CreatedAt: time.Now().UTC(),
			Raw:       body,
		}, nil
	}

	px := "0"
	tif := "Ioc"
	if req.OrderType.IsLimit() {
		px = strconv.FormatFloat(req.LimitPrice, 'f', -1, 64)
		tif = "Gtc"
	}
	action := orderAction{
		Type: "order",
		Orders: []orderLeg{{
			A: idx,
			B: req.Side == trade.SideBuy,
			P: px,
			S: qtyString(req.Quantity),
			R: false,
			T: orderTypeBox{Limit: orderLimit{Tif: tif}},
		}},
		Grouping: "na",
	}
	body, err := a.postAction(ctx, creds, action)
	if err != nil {
		return nil, err
	}
	oid, avgPx, perr := extractOrderResult(body)
	if perr != nil {
		return nil, perr
	}
	return &trade.Result{
		OrderID:   oid,
		Symbol:    req.Symbol,
		Side:      req.Side,
		Quantity:  req.Quantity,
		AvgPrice:  avgPx,
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
	action := orderAction{
		Type: "order",
		Orders: []orderLeg{{
			A: idx,
			B: closeIsBuy,
			P: "0",
			S: qtyString(p.Quantity),
			R: true,
			T: orderTypeBox{Limit: orderLimit{Tif: "Ioc"}},
		}},
		Grouping: "na",
	}
	body, err := a.postAction(ctx, creds, action)
	if err != nil {
		return nil, err
	}
	oid, avgPx, perr := extractOrderResult(body)
	if perr != nil {
		return nil, perr
	}
	closeSide := trade.SideSell
	if closeIsBuy {
		closeSide = trade.SideBuy
	}
	return &trade.Result{
		OrderID:   oid,
		Symbol:    req.Symbol,
		Side:      closeSide,
		Quantity:  p.Quantity,
		AvgPrice:  avgPx,
		Status:    "NEW",
		CreatedAt: time.Now().UTC(),
		Raw:       body,
	}, nil
}

// extractOrderResult parses oid and avgPx from HL's response envelope:
//
//	{"status":"ok","response":{"data":{"statuses":[{"resting":{"oid":...}}|{"filled":{"oid":...,"avgPx":"..."}}|{"error":"..."}]}}}
//
// Returns a non-nil error when HL set status="error" so the caller can
// surface the rejection (without this the dispatcher reported a clean
// "NEW" status with empty order_id whenever HL rejected the order).
func extractOrderResult(body []byte) (oid string, avgPx float64, err error) {
	var env struct {
		Status   string `json:"status"`
		Response struct {
			Data struct {
				Statuses []map[string]json.RawMessage `json:"statuses"`
			} `json:"data"`
		} `json:"response"`
	}
	if e := json.Unmarshal(body, &env); e != nil {
		err = e
		return
	}
	if env.Status != "" && env.Status != "ok" {
		err = &trade.Error{Kind: trade.KindExchange,
			Message: fmt.Sprintf("hyperliquid status=%s body=%s", env.Status, string(body))}
		return
	}
	if len(env.Response.Data.Statuses) == 0 {
		return
	}
	first := env.Response.Data.Statuses[0]
	if raw, ok := first["error"]; ok {
		var msg string
		if e := json.Unmarshal(raw, &msg); e != nil {
			msg = string(raw)
		}
		err = &trade.Error{Kind: trade.KindExchange, Message: "hyperliquid rejected: " + msg}
		return
	}
	for _, key := range []string{"resting", "filled"} {
		raw, ok := first[key]
		if !ok {
			continue
		}
		var inner struct {
			Oid   json.Number `json:"oid"`
			AvgPx json.Number `json:"avgPx"`
		}
		if e := json.Unmarshal(raw, &inner); e == nil {
			oid = inner.Oid.String()
			avgPx, _ = inner.AvgPx.Float64()
			return
		}
	}
	return
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
				Coin          string      `json:"coin"`
				Szi           json.Number `json:"szi"`
				EntryPx       json.Number `json:"entryPx"`
				PositionValue json.Number `json:"positionValue"`
				UnrealizedPnl json.Number `json:"unrealizedPnl"`
				Leverage      struct {
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
	s := strings.TrimRight(strings.TrimRight(
		fmtFloat(q), "0"), ".")
	if s == "" {
		s = "0"
	}
	return s
}

func fmtFloat(q float64) string {
	// Up to 8 decimals, no exponent.
	return new(big.Float).SetFloat64(q).Text('f', 8)
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
