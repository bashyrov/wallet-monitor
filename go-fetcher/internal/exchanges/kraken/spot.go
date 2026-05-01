// Kraken spot orderbook WS (V2 protocol).
//
// URL: wss://ws.kraken.com/v2 (different host from futures.kraken.com).
// Symbol form: `BTC/USD` (with slash, no PF_ prefix). Same XBT alias
// for BTC at the wire level.
//
// Subscribe shape:
//   {"method":"subscribe","params":{"channel":"book",
//    "symbol":["BTC/USD","ETH/USD",...], "depth":100}}
//
// Inbound:
//   {"channel":"book","type":"snapshot",
//    "data":[{"symbol":"BTC/USD",
//             "bids":[{"price":...,"qty":...},...],
//             "asks":[...]}]}
//   ... and "type":"update" for deltas (single symbol per frame).
package kraken

import (
	"context"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const spotWS = "wss://ws.kraken.com/v2"

type Spot struct {
	store *cache.Store
	books map[string]*book
}

func NewSpot(store *cache.Store) *ws.Runner {
	a := &Spot{store: store, books: make(map[string]*book)}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("kraken_spot", snap.Symbol, snap, "ws")
	})
}

func (a *Spot) Name() string                          { return "kraken_spot" }
func (a *Spot) URL(_ context.Context) (string, error) { return spotWS, nil }

func (a *Spot) BuildSubscribe(symbols []string) [][]byte {
	pairs := make([]string, len(symbols))
	for i, s := range symbols {
		token := strings.ToUpper(s)
		if token == "BTC" {
			token = "XBT"
		}
		pairs[i] = token + "/USD"
	}
	frame := map[string]any{
		"method": "subscribe",
		"params": map[string]any{
			"channel": "book",
			"symbol":  pairs,
			"depth":   100,
		},
	}
	b, _ := ws.MarshalJSON(frame)
	return [][]byte{b}
}

func (a *Spot) Parse(frame []byte) (*ws.Snapshot, error) {
	var msg struct {
		Channel string `json:"channel"`
		Type    string `json:"type"` // snapshot | update
		Method  string `json:"method"`
		Data    []struct {
			Symbol string `json:"symbol"`
			Bids   []struct {
				Price float64 `json:"price"`
				Qty   float64 `json:"qty"`
			} `json:"bids"`
			Asks []struct {
				Price float64 `json:"price"`
				Qty   float64 `json:"qty"`
			} `json:"asks"`
		} `json:"data"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Method != "" {
		return nil, nil // status / subscribe-ack frames
	}
	if msg.Channel != "book" {
		return nil, nil
	}
	if len(msg.Data) == 0 {
		return nil, nil
	}
	d := msg.Data[0]
	if !strings.HasSuffix(d.Symbol, "/USD") {
		return nil, nil
	}
	token := strings.TrimSuffix(d.Symbol, "/USD")
	if token == "XBT" {
		token = "BTC"
	}

	bk, ok := a.books[token]
	if !ok {
		bk = &book{bids: make(map[float64]float64), asks: make(map[float64]float64)}
		a.books[token] = bk
	}
	if msg.Type == "snapshot" {
		bk.bids = make(map[float64]float64, len(d.Bids))
		bk.asks = make(map[float64]float64, len(d.Asks))
	}
	for _, b := range d.Bids {
		if b.Qty == 0 {
			delete(bk.bids, b.Price)
		} else {
			bk.bids[b.Price] = b.Qty
		}
	}
	for _, ak := range d.Asks {
		if ak.Qty == 0 {
			delete(bk.asks, ak.Price)
		} else {
			bk.asks[ak.Price] = ak.Qty
		}
	}
	return &ws.Snapshot{
		Symbol: token,
		Bids:   ws.SortedLevels(bk.bids, ws.Bids, 200),
		Asks:   ws.SortedLevels(bk.asks, ws.Asks, 200),
	}, nil
}

func (a *Spot) Heartbeat() []byte                { return nil }
func (a *Spot) HeartbeatInterval() time.Duration { return 0 }
func (a *Spot) PongFor(_ []byte) []byte          { return nil }
func (a *Spot) UseLibPings() bool                { return true }
func (a *Spot) SubscribeDelay() time.Duration    { return 0 }
func (a *Spot) MaxSymbols() int                  { return 0 }
func (a *Spot) DecompressGzip() bool             { return false }
func (a *Spot) OnReconnect()                     { a.books = make(map[string]*book) }
