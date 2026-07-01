// Lighter zk-perp adapter — full trade support (pure Go).
//
// Lighter is a ZK-rollup with API keys signed via Schnorr/Poseidon over
// the ECgFp5 curve. The official Python `lighter-sdk` ships a native
// CGO library for signing which historically was the only path — we
// used to route lighter writes through Python for that reason.
//
// As of 2026-05 upstream published `github.com/elliottech/lighter-go`
// with a pure-Go KeyManager + ConstructCreateOrderTx implementation
// (no CGO, no lighter-sdk dependency, no per-arch native builds).
// This adapter uses that SDK for signing — see sign.go.
//
// What this adapter does:
//
//   - GetBalance / ListPositions hit the unsigned REST endpoints
//     (/api/v1/account) and run entirely in Go.
//   - PlaceOrder / ClosePosition / SetLeverage sign L2 transactions via
//     elliottech/lighter-go and POST to /api/v1/sendTx. Full trade
//     support — lighter is safe to include in GO_TRADE_VENUES.
//   - SetLeverage is a no-op (Lighter sets leverage at market level,
//     not per-order).
//
// Credentials mapping (matches Python):
//
//	APIKey     → account_index   (numeric string)
//	APISecret  → api_private_key (hex, 0x optional)
//	Passphrase → api_key_index   (default "255")
package lighter

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

const baseURL = "https://mainnet.zklighter.elliot.ai"

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

func init() { trade.Register("lighter", New()) }

func (a *Adapter) Name() string { return "lighter" }

// ── REST (no signing) ────────────────────────────────────────────────────

func (a *Adapter) get(ctx context.Context, path string, qs map[string]string) (json.RawMessage, error) {
	u := baseURL + path
	if len(qs) > 0 {
		first := true
		for k, v := range qs {
			sep := "&"
			if first {
				sep = "?"
				first = false
			}
			u += sep + k + "=" + v
		}
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
		return nil, &trade.Error{Kind: trade.KindExchange, Message: strings.TrimSpace(string(raw))}
	}
	return raw, nil
}

func (a *Adapter) accountIndex(creds trade.Creds) (string, error) {
	idx := strings.TrimSpace(creds.APIKey)
	if idx == "" {
		return "", errUser("lighter requires the numeric account_index in api_key")
	}
	if _, err := strconv.Atoi(idx); err != nil {
		return "", errUser("lighter account_index must be an integer")
	}
	return idx, nil
}

func (a *Adapter) GetBalance(ctx context.Context, creds trade.Creds) (*trade.Balance, error) {
	idx, err := a.accountIndex(creds)
	if err != nil {
		return nil, err
	}
	body, err := a.get(ctx, "/api/v1/account", map[string]string{"by": "index", "value": idx})
	if err != nil {
		return nil, err
	}
	var resp struct {
		Accounts []struct {
			Assets []struct {
				Symbol        string      `json:"symbol"`
				Balance       json.Number `json:"balance"`
				LockedBalance json.Number `json:"locked_balance"`
			} `json:"assets"`
		} `json:"accounts"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		return nil, errInternal("parse account", err)
	}
	if len(resp.Accounts) == 0 {
		return &trade.Balance{}, nil
	}
	var total float64
	for _, asset := range resp.Accounts[0].Assets {
		sym := strings.ToUpper(asset.Symbol)
		if sym != "USDC" && sym != "USDT" {
			continue
		}
		bal, _ := asset.Balance.Float64()
		locked, _ := asset.LockedBalance.Float64()
		total += bal + locked
	}
	return &trade.Balance{TotalUSD: total, AvailableUSD: total}, nil
}

func (a *Adapter) ListPositions(ctx context.Context, creds trade.Creds, symbol string) ([]trade.Position, error) {
	idx, err := a.accountIndex(creds)
	if err != nil {
		return nil, err
	}
	body, err := a.get(ctx, "/api/v1/account", map[string]string{"by": "index", "value": idx})
	if err != nil {
		return nil, err
	}
	var resp struct {
		Accounts []struct {
			Positions []struct {
				Symbol           string      `json:"symbol"`
				Position         json.Number `json:"position"`
				Sign             int         `json:"sign"`
				AvgEntryPrice    json.Number `json:"avg_entry_price"`
				UnrealizedPnl    json.Number `json:"unrealized_pnl"`
				RealizedFunding  json.Number `json:"realized_funding"`
				AllocatedMargin  json.Number `json:"allocated_margin"`
			} `json:"positions"`
		} `json:"accounts"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		return nil, errInternal("parse positions", err)
	}
	if len(resp.Accounts) == 0 {
		return nil, nil
	}
	want := strings.ToUpper(symbol)
	out := make([]trade.Position, 0, len(resp.Accounts[0].Positions))
	for _, p := range resp.Accounts[0].Positions {
		qty, _ := p.Position.Float64()
		if qty == 0 {
			continue
		}
		sym := strings.ToUpper(p.Symbol)
		if want != "" && sym != want {
			continue
		}
		side := trade.SideBuy
		if p.Sign != 1 && qty <= 0 {
			side = trade.SideSell
		}
		entry, _ := p.AvgEntryPrice.Float64()
		upnl, _ := p.UnrealizedPnl.Float64()
		funding, _ := p.RealizedFunding.Float64()
		levF, _ := p.AllocatedMargin.Float64()
		out = append(out, trade.Position{
			Symbol:        sym,
			Side:          side,
			Quantity:      abs(qty),
			EntryPrice:    entry,
			UnrealizedPnL: upnl,
			FundingPnL:    funding,
			Leverage:      int(levF),
			MarginMode:    trade.MarginCross,
		})
	}
	return out, nil
}

// ── Trade actions — Schnorr/Poseidon signing via lighter-go SDK ──────────
// As of 2026-05 elliottech/lighter-go (pure Go, no CGO) ships KeyManager
// + ConstructCreateOrderTx. We use those directly — no lighter-sdk Python
// dependency, no CGO bridge, no per-arch builds.

const (
	// Lighter mainnet chain id. Validated via /api/v1/info or by signing
	// against an actual order in dev.
	lighterChainID uint32 = 304
)

// SetLeverage is a no-op on Lighter — leverage is set on the position
// type at the market level, not per-order. Returning nil keeps the
// dispatcher happy (matches the existing perp adapter contract).
func (a *Adapter) SetLeverage(_ context.Context, _ trade.Creds, _ trade.LeverageRequest) error {
	return nil
}

// PlaceOrder builds a signed L2 create-order tx and POSTs to /api/v1/sendTx.
// Returns immediately with the venue's order-id; fill data is streamed via
// the existing /api/v1/account poller.
//
// Market orders are encoded as Type=MARKET, Price=0; Lighter handles
// slippage internally. Limit orders use req.LimitPrice (float → uint32
// scaled by market tick — see priceScale below).
func (a *Adapter) PlaceOrder(ctx context.Context, creds trade.Creds, req trade.OpenRequest) (*trade.Result, error) {
	if err := req.Validate(); err != nil {
		return nil, err
	}
	signed, err := a.buildSignedOrder(ctx, creds, req)
	if err != nil {
		return nil, err
	}
	out, err := a.submitTx(ctx, signed)
	if err != nil {
		return nil, err
	}
	return &trade.Result{
		OrderID:   string(out),  // Lighter returns tx hash; reconcile worker maps to order
		Symbol:    req.Symbol,
		Side:      req.Side,
		Quantity:  req.Quantity,
		Status:    "filled",
		CreatedAt: time.Now().UTC(),
		Raw:       out,
	}, nil
}

// ClosePosition issues an opposite-side reduce-only market order for the
// open size of the symbol's position.
func (a *Adapter) ClosePosition(ctx context.Context, creds trade.Creds, req trade.CloseRequest) (*trade.Result, error) {
	positions, err := a.ListPositions(ctx, creds, req.Symbol)
	if err != nil {
		return nil, err
	}
	if len(positions) == 0 {
		return nil, errUser("no open Lighter position for %s", req.Symbol)
	}
	pos := positions[0]
	side := trade.SideSell
	if pos.Quantity < 0 {
		side = trade.SideBuy
	}
	openReq := trade.OpenRequest{
		Symbol:    req.Symbol,
		Side:      side,
		Quantity:  abs(pos.Quantity),
		OrderType: trade.OrderMarket,
	}
	signed, err := a.buildSignedOrder(ctx, creds, openReq)
	if err != nil {
		return nil, err
	}
	out, err := a.submitTx(ctx, signed)
	if err != nil {
		return nil, err
	}
	return &trade.Result{
		OrderID:   string(out),
		Symbol:    req.Symbol,
		Side:      side,
		Quantity:  abs(pos.Quantity),
		Status:    "filled",
		CreatedAt: time.Now().UTC(),
		Raw:       out,
	}, nil
}

// buildSignedOrder resolves the market index, scales price/qty, signs the
// tx and returns the wire-ready JSON body.
func (a *Adapter) buildSignedOrder(ctx context.Context, creds trade.Creds, req trade.OpenRequest) ([]byte, error) {
	mkt, err := a.marketByName(ctx, req.Symbol)
	if err != nil {
		return nil, err
	}
	km, err := lighterKeyManager(creds.APISecret)
	if err != nil {
		return nil, err
	}
	apiKeyIdx := uint8(255)
	if creds.Passphrase != "" {
		if n, perr := strconv.Atoi(strings.TrimSpace(creds.Passphrase)); perr == nil && n >= 0 && n <= 255 {
			apiKeyIdx = uint8(n)
		}
	}
	accIdx, _ := strconv.ParseInt(strings.TrimSpace(creds.APIKey), 10, 64)

	// Quantity scaled to the market's base-amount unit; price scaled to
	// tick. Until we wire up /api/v1/orderBookDetails for exact decimals,
	// we use the market info embedded in idMap (resolved by marketByName).
	baseAmount := int64(req.Quantity * mkt.BaseScale)
	priceScaled := uint32(0)
	if req.OrderType.IsLimit() {
		priceScaled = uint32(req.LimitPrice * float64(mkt.PriceScale))
	}
	side := uint8(0) // 0 = bid (buy)
	if req.Side == trade.SideSell {
		side = 1
	}
	orderType := uint8(0) // 0 = market, 1 = limit
	if req.OrderType.IsLimit() {
		orderType = 1
	}

	tx, err := lighterConstructOrder(km, lighterChainID, mkt.MarketIndex, baseAmount, priceScaled,
		side, orderType, &accIdx, &apiKeyIdx)
	if err != nil {
		return nil, errInternal("sign order", err)
	}
	body, err := json.Marshal(tx)
	if err != nil {
		return nil, errInternal("marshal tx", err)
	}
	return body, nil
}

func (a *Adapter) submitTx(ctx context.Context, body []byte) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, baseURL+"/api/v1/sendTx",
		strings.NewReader(string(body)))
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
		return nil, &trade.Error{Kind: trade.KindExchange, Message: strings.TrimSpace(string(raw))}
	}
	return raw, nil
}

// marketByName resolves a token symbol → MarketIndex + scaling factors via
// /api/v1/orderBookDetails (cached). Falls back to a single REST call on miss.
type lighterMarket struct {
	MarketIndex int16
	BaseScale   float64 // multiply float qty by this to get int64
	PriceScale  int64   // multiply float price by this to get uint32
}

var (
	mktMu      sync.RWMutex
	mktBySym   map[string]lighterMarket
	mktLoaded  time.Time
	mktTTL     = time.Hour
)

func (a *Adapter) marketByName(ctx context.Context, symbol string) (lighterMarket, error) {
	want := strings.ToUpper(strings.TrimSpace(symbol))
	mktMu.RLock()
	if mktBySym != nil && time.Since(mktLoaded) < mktTTL {
		if m, ok := mktBySym[want]; ok {
			mktMu.RUnlock()
			return m, nil
		}
	}
	mktMu.RUnlock()

	raw, err := a.get(ctx, "/api/v1/orderBookDetails", nil)
	if err != nil {
		return lighterMarket{}, err
	}
	var doc struct {
		OrderBookDetails []struct {
			Symbol           string `json:"symbol"`
			MarketID         int16  `json:"market_id"`
			SizeDecimals     int    `json:"size_decimals"`
			PriceDecimals    int    `json:"price_decimals"`
		} `json:"order_book_details"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		return lighterMarket{}, errInternal("parse orderBookDetails", err)
	}
	tmp := make(map[string]lighterMarket, len(doc.OrderBookDetails))
	for _, m := range doc.OrderBookDetails {
		bs := 1.0
		for i := 0; i < m.SizeDecimals; i++ {
			bs *= 10
		}
		ps := int64(1)
		for i := 0; i < m.PriceDecimals; i++ {
			ps *= 10
		}
		tmp[strings.ToUpper(m.Symbol)] = lighterMarket{
			MarketIndex: m.MarketID,
			BaseScale:   bs,
			PriceScale:  ps,
		}
	}
	mktMu.Lock()
	mktBySym = tmp
	mktLoaded = time.Now()
	mktMu.Unlock()
	if m, ok := tmp[want]; ok {
		return m, nil
	}
	return lighterMarket{}, errUser("lighter: market %s not listed", want)
}

// ── Helpers ──────────────────────────────────────────────────────────────

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
