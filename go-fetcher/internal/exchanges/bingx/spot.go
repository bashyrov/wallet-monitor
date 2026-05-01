// BingX spot orderbook WS.
//
// URL: wss://open-api-ws.bingx.com/market (different host from
// open-api-swap.bingx.com/swap-market used for futures).
//
// Subscribe / parse / keepalive shape are identical to futures — gzip
// frames, "Ping"/"Pong" text echo, dataType "<BASE>-USDT@depth20".
//
// 100-symbol cap per connection (Bug #5) applies the same way.
package bingx

import (
	"context"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const spotWS = "wss://open-api-ws.bingx.com/market"

type Spot struct{ *Futures }

func NewSpot(store *cache.Store) *ws.Runner {
	parent := &Futures{store: store}
	a := &Spot{Futures: parent}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("bingx_spot", snap.Symbol, snap, "ws")
	})
}

func (a *Spot) Name() string                          { return "bingx_spot" }
func (a *Spot) URL(_ context.Context) (string, error) { return spotWS, nil }
