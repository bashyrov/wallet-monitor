// Bybit V5 spot orderbook WS.
//
// URL: wss://stream.bybit.com/v5/public/spot (public/linear was the
// futures host).
//
// Channel + frame format are identical to futures — same topic prefix
// (`orderbook.50.{symbol}USDT`), same snapshot+delta protocol. The only
// behavioural difference at the API level is the WS host, so we reuse
// Futures' Parse/BuildSubscribe via type embedding.
package bybit

import (
	"context"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const spotWS = "wss://stream.bybit.com/v5/public/spot"

type Spot struct{ *Futures }

func NewSpot(store *cache.Store) *ws.Runner {
	parent := &Futures{store: store, books: make(map[string]*book)}
	a := &Spot{Futures: parent}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("bybit_spot", snap.Symbol, snap, "ws")
	})
}

func (a *Spot) Name() string                          { return "bybit_spot" }
func (a *Spot) URL(_ context.Context) (string, error) { return spotWS, nil }
