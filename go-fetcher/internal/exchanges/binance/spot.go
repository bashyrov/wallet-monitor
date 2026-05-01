// Binance spot orderbook WS.
//
// URL: wss://stream.binance.com:9443/ws (different host from fapi)
// Channel: <symbol-lower>@depth20@100ms — same shape as futures (full
// snapshot every 100ms, 20 levels per side). Subscribe via SUBSCRIBE
// frame after connect.
//
// Diverges from futures only in the WS host. The frame format and
// SUBSCRIBE shape are identical, so we share Parse/BuildSubscribe via
// a Spot type that wraps Futures' parsing logic but advertises a
// different cache key (`binance_spot`) and URL.
package binance

import (
	"context"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const spotWS = "wss://stream.binance.com:9443/ws"

// Spot is the WS adapter for Binance spot orderbook. Reuses the futures
// Parse/BuildSubscribe machinery — the on-the-wire format is identical
// across the two product lines.
type Spot struct{ *Futures }

func NewSpot(store *cache.Store) *ws.Runner {
	// Parent Futures struct is what holds the trading filter + state.
	// We pass a sentinel filter that always trades — Binance Spot has
	// far fewer delisted-but-still-streamed quirks than the futures
	// product, and the spot-arb compute already filters by REST
	// exchangeInfo upstream.
	parent := &Futures{store: store, filter: NewSpotTradingFilter()}
	a := &Spot{Futures: parent}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("binance_spot", snap.Symbol, snap, "ws")
	})
}

func (a *Spot) Name() string                          { return "binance_spot" }
func (a *Spot) URL(_ context.Context) (string, error) { return spotWS, nil }

// BuildSubscribe — same layout as futures. Override only because we need
// the receiver type to be *Spot for the runner to bind it.
func (a *Spot) BuildSubscribe(symbols []string) [][]byte {
	if len(symbols) == 0 {
		return nil
	}
	params := make([]string, len(symbols))
	for i, s := range symbols {
		params[i] = strings.ToLower(s) + "usdt@depth20@100ms"
	}
	frame := map[string]any{
		"method": "SUBSCRIBE",
		"params": params,
		"id":     time.Now().UnixNano(),
	}
	b, _ := ws.MarshalJSON(frame)
	return [][]byte{b}
}
