// Package trade — Go port of backend/services/trade_adapters.
//
// Goal: lift `place_order` / `close_position` / `set_leverage` off the
// Python event loop. The Python web role currently spends 200–800 ms in
// asyncio.gather signing + sending two legs of a pair-open; the
// signing CPU + GIL contention is the dominant cost. Go does both in
// real parallel goroutines and the network round-trip dominates again.
//
// Surface that mirrors Python (`trade_adapters/_base.py`):
//
//	type Adapter interface {
//	    PlaceOrder(ctx, creds, req)    → Result
//	    ClosePosition(ctx, creds, req) → Result
//	    SetLeverage(ctx, creds, req)   → error
//	    ListPositions(ctx, creds, sym) → []Position
//	    Balance(ctx, creds)            → Balance
//	}
//
// Migration plan (see TRADE_PORT.md):
//   1. Foundation + Binance reference (this commit).
//   2. Bybit / Hyperliquid (next two most-used venues).
//   3. Remaining 13 venues — one per commit, each gated by a feature
//      flag so a regression on one venue can be hot-toggled off.
package trade

import (
	"context"
	"encoding/json"
	"errors"
	"time"
)

// Side matches Python's trade_service contract: "buy" opens long,
// "sell" opens short. close_position takes the same side as the
// position to close.
type Side string

const (
	SideBuy  Side = "buy"
	SideSell Side = "sell"
)

func (s Side) IsValid() bool { return s == SideBuy || s == SideSell }

// MarginMode matches every exchange's two-tier model.
type MarginMode string

const (
	MarginIsolated MarginMode = "isolated"
	MarginCross    MarginMode = "cross"
)

func (m MarginMode) IsValid() bool { return m == MarginIsolated || m == MarginCross }

// MarketType selects spot vs perp/futures order routing on venues that
// support both. Default zero-value "" treated as MarketFutures for
// backward compatibility (every existing call site is futures-only).
type MarketType string

const (
	MarketFutures MarketType = "futures"
	MarketSpot    MarketType = "spot"
)

func (m MarketType) IsSpot() bool { return m == MarketSpot }

// Creds — flattened per-exchange credential bag. Mirror of Python's
// `decrypt_credentials(w.credentials)` output. Fields not used by an
// exchange are simply ignored (e.g. Hyperliquid uses Wallet for sk).
type Creds struct {
	APIKey     string `json:"api_key,omitempty"`
	APISecret  string `json:"api_secret,omitempty"`
	Passphrase string `json:"passphrase,omitempty"`     // OKX, KuCoin, Bitget
	Wallet     string `json:"wallet,omitempty"`         // Hyperliquid Stark address
	PrivateKey string `json:"private_key,omitempty"`    // Hyperliquid signer
	UID        string `json:"uid,omitempty"`            // KuCoin sub-account, etc.
	Extra      map[string]string `json:"extra,omitempty"` // future-proof
}

// OpenRequest mirrors Python `place_order(creds, symbol, side, qty,
// leverage, margin_mode)`. Only fields that vary per call.
//
// MarketType is futures-by-default (zero-value resolves to "futures")
// so every existing caller works unchanged. Set MarketSpot to route
// the order through the venue's spot adapter where supported. Spot
// orders ignore Leverage + MarginMode (spot is always 1× / cash).
type OpenRequest struct {
	Symbol     string     `json:"symbol"`
	Side       Side       `json:"side"`
	Quantity   float64    `json:"quantity"`
	Leverage   int        `json:"leverage"`
	MarginMode MarginMode `json:"margin_mode"`
	MarketType MarketType `json:"market_type,omitempty"`
}

func (r OpenRequest) Validate() error {
	if r.Symbol == "" {
		return errUser("symbol required")
	}
	if !r.Side.IsValid() {
		return errUser("side must be buy/sell")
	}
	if r.Quantity <= 0 {
		return errUser("quantity must be > 0")
	}
	// Leverage + margin only required on futures. Spot is always 1× / cash
	// — no preflight calls, no hedge-mode dance, just a single signed POST.
	if !r.MarketType.IsSpot() {
		if r.Leverage <= 0 {
			return errUser("leverage must be > 0")
		}
		if !r.MarginMode.IsValid() {
			return errUser("margin_mode must be isolated/cross")
		}
	}
	return nil
}

// CloseRequest — close a position on (symbol, side). Side is the
// SAME side as the position to close (Python's contract). When the
// exchange uses one-way mode and `side` is "", the adapter resolves
// it from list_positions.
//
// For spot, "close" means "sell the long position" — the adapter
// computes available base-asset balance and sells it as a market order.
// Side on spot CloseRequest is ignored (spot can't be short).
type CloseRequest struct {
	Symbol     string     `json:"symbol"`
	Side       Side       `json:"side"`
	MarketType MarketType `json:"market_type,omitempty"`
}

// LeverageRequest — set isolated/cross leverage for one symbol. Idempotent.
type LeverageRequest struct {
	Symbol     string     `json:"symbol"`
	Leverage   int        `json:"leverage"`
	MarginMode MarginMode `json:"margin_mode"`
}

// Result — common response shape for place_order / close_position.
// Free-form Raw for venue-specific fields the UI may need.
type Result struct {
	OrderID       string          `json:"order_id,omitempty"`
	Symbol        string          `json:"symbol"`
	Side          Side            `json:"side"`
	Quantity      float64         `json:"quantity"`
	AvgPrice      float64         `json:"avg_price,omitempty"`
	Status        string          `json:"status,omitempty"`        // FILLED / NEW / PARTIALLY_FILLED
	ClientOrderID string          `json:"client_order_id,omitempty"`
	CreatedAt     time.Time       `json:"created_at,omitempty"`
	Raw           json.RawMessage `json:"raw,omitempty"`           // venue-native payload
}

// Position — what `list_positions` returns. Same shape Python uses to
// power the /trade/positions endpoint.
type Position struct {
	Symbol         string   `json:"symbol"`
	Side           Side     `json:"side"`            // buy=long, sell=short
	Quantity       float64  `json:"quantity"`        // contract size
	Notional       float64  `json:"notional_usd,omitempty"`
	EntryPrice     float64  `json:"entry_price,omitempty"`
	MarkPrice      float64  `json:"mark_price,omitempty"`
	Leverage       int      `json:"leverage,omitempty"`
	UnrealizedPnL  float64  `json:"unrealized_pnl_usd,omitempty"`
	RealizedPnL    float64  `json:"realized_pnl_usd,omitempty"`
	FundingPnL     float64  `json:"funding_pnl_usd,omitempty"`
	MarginMode     MarginMode `json:"margin_mode,omitempty"`
	OpenedAt       time.Time `json:"opened_at,omitempty"`
}

// Balance — cash available to open new positions, as USD-equivalent.
// Mirror of Python's `fetch_balance` output.
type Balance struct {
	TotalUSD     float64 `json:"total_usd"`
	AvailableUSD float64 `json:"available_usd"`
	MarginUSD    float64 `json:"margin_usd,omitempty"`
}

// Adapter — the per-exchange interface every venue implements.
// Mirror of Python's `_base.py` REQUIRED_METHODS contract, but with
// context.Context for cancellation and concrete request structs.
type Adapter interface {
	Name() string
	PlaceOrder(ctx context.Context, creds Creds, req OpenRequest) (*Result, error)
	ClosePosition(ctx context.Context, creds Creds, req CloseRequest) (*Result, error)
	SetLeverage(ctx context.Context, creds Creds, req LeverageRequest) error
	ListPositions(ctx context.Context, creds Creds, symbol string) ([]Position, error)
	GetBalance(ctx context.Context, creds Creds) (*Balance, error)
}

// SpotAdapter — optional venue extension for spot order routing. Adapters
// that implement this also support PlaceOrder/ClosePosition for the same
// venue's perp/futures (Adapter is required). The dispatcher checks for
// this at request time when req.MarketType == MarketSpot. Adapters that
// don't implement it return ErrSpotUnsupported and the dispatcher returns
// a 4xx to the caller.
type SpotAdapter interface {
	PlaceSpotOrder(ctx context.Context, creds Creds, req OpenRequest) (*Result, error)
	CloseSpotPosition(ctx context.Context, creds Creds, req CloseRequest) (*Result, error)
}

// Sentinel — adapter not registered for the requested exchange.
var ErrUnsupported = errors.New("exchange not supported by Go trade engine")
var ErrSpotUnsupported = errors.New("spot trading not supported on this venue (yet)")
