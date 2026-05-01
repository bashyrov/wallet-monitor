// Gate.io spot orderbook WS.
//
// URL: wss://api.gateio.ws/ws/v4/ (different host from fx-ws.gateio.ws
// used for futures).
// Channel: spot.order_book — payload [currency_pair, level, interval]
//   {"time": N, "channel":"spot.order_book", "event":"subscribe",
//    "payload":["BTC_USDT","20","100ms"]}
//
// Inbound shape (spot returns full snapshot every push, no "all"/"update"
// distinction — just channel="spot.order_book", event="update"):
//
//   {"time": N, "channel":"spot.order_book", "event":"update",
//    "result":{"t":..., "lastUpdateId":..., "s":"BTC_USDT",
//              "bids":[["price","amount"], ...],
//              "asks":[["price","amount"], ...]}}
//
// Bids/asks are arrays-of-strings here (futures used the {p,s} object
// form), so Parse is not shareable with the futures adapter — separate
// implementation.
package gate

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const spotWS = "wss://api.gateio.ws/ws/v4/"

type Spot struct {
	store *cache.Store
	books map[string]*book
}

func NewSpot(store *cache.Store) *ws.Runner {
	a := &Spot{store: store, books: make(map[string]*book)}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("gate_spot", snap.Symbol, snap, "ws")
	})
}

func (a *Spot) Name() string                          { return "gate_spot" }
func (a *Spot) URL(_ context.Context) (string, error) { return spotWS, nil }

func (a *Spot) BuildSubscribe(symbols []string) [][]byte {
	frames := make([][]byte, 0, len(symbols))
	for _, s := range symbols {
		f := map[string]any{
			"time":    time.Now().Unix(),
			"channel": "spot.order_book",
			"event":   "subscribe",
			"payload": []string{strings.ToUpper(s) + "_USDT", "20", "100ms"},
		}
		b, _ := ws.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

func (a *Spot) Parse(frame []byte) (*ws.Snapshot, error) {
	var msg struct {
		Channel string `json:"channel"`
		Event   string `json:"event"`
		Result  struct {
			Symbol string     `json:"s"`
			Bids   [][]string `json:"bids"`
			Asks   [][]string `json:"asks"`
		} `json:"result"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Channel != "spot.order_book" {
		return nil, nil
	}
	if msg.Event != "update" && msg.Event != "all" {
		return nil, nil
	}
	if !strings.HasSuffix(msg.Result.Symbol, "_USDT") {
		return nil, nil
	}
	token := strings.TrimSuffix(msg.Result.Symbol, "_USDT")

	bk, ok := a.books[token]
	if !ok {
		bk = &book{bids: make(map[float64]float64), asks: make(map[float64]float64)}
		a.books[token] = bk
	}
	// Gate spot.order_book at 100ms interval pushes full top-N every
	// tick — treat each frame as a snapshot.
	bk.bids = make(map[float64]float64, len(msg.Result.Bids))
	bk.asks = make(map[float64]float64, len(msg.Result.Asks))
	for _, r := range msg.Result.Bids {
		if len(r) < 2 {
			continue
		}
		px, _ := strconv.ParseFloat(r[0], 64)
		sz, _ := strconv.ParseFloat(r[1], 64)
		if sz > 0 {
			bk.bids[px] = sz
		}
	}
	for _, r := range msg.Result.Asks {
		if len(r) < 2 {
			continue
		}
		px, _ := strconv.ParseFloat(r[0], 64)
		sz, _ := strconv.ParseFloat(r[1], 64)
		if sz > 0 {
			bk.asks[px] = sz
		}
	}
	return &ws.Snapshot{
		Symbol: token,
		Bids:   ws.SortedLevels(bk.bids, ws.Bids, 200),
		Asks:   ws.SortedLevels(bk.asks, ws.Asks, 200),
	}, nil
}

// Same keepalive policy as futures — gorilla lib pings.
func (a *Spot) Heartbeat() []byte                { return nil }
func (a *Spot) HeartbeatInterval() time.Duration { return 0 }
func (a *Spot) PongFor(_ []byte) []byte          { return nil }
func (a *Spot) UseLibPings() bool                { return true }
func (a *Spot) SubscribeDelay() time.Duration    { return 0 }
func (a *Spot) MaxSymbols() int                  { return 0 }
func (a *Spot) DecompressGzip() bool             { return false }
func (a *Spot) OnReconnect() {
	a.books = make(map[string]*book)
}
