// Package gate — Gate.io USDT-margined perp.
//
// URL: wss://fx-ws.gateio.ws/v4/ws/usdt
// Channel: futures.order_book — payload [contract, depth, interval]
//   {"time":N, "channel":"futures.order_book", "event":"subscribe",
//    "payload":["BTC_USDT","20","100ms"]}
//
// Inbound:
//   {"time":N, "channel":"futures.order_book", "event":"all"|"update",
//    "result":{"contract":"BTC_USDT","bids":[{"p":"...","s":N}, ...], "asks":[...]}}
//
// "all" carries a snapshot, "update" the deltas (size 0 = remove).
package gate

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const futuresWS = "wss://fx-ws.gateio.ws/v4/ws/usdt"

type Futures struct {
	store *cache.Store
	books map[string]*book
}

type book struct {
	bids map[float64]float64
	asks map[float64]float64
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{store: store, books: make(map[string]*book)}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("gate", snap.Symbol, snap, "ws")
	})
}

func (a *Futures) Name() string                          { return "gate" }
func (a *Futures) URL(_ context.Context) (string, error) { return futuresWS, nil }

func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
	frames := make([][]byte, 0, len(symbols))
	for _, s := range symbols {
		// Gate accuracy: "0" = push on every change (probed: "100ms"
		// returns "invalid accuracy", only "0" / "0.1" / "0.01" forms
		// accepted; "0" gives finest precision).
		f := map[string]any{
			"time":    time.Now().Unix(),
			"channel": "futures.order_book",
			"event":   "subscribe",
			"payload": []string{strings.ToUpper(s) + "_USDT", "20", "0"},
		}
		b, _ := ws.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
	var msg struct {
		Channel string `json:"channel"`
		Event   string `json:"event"`
		Result  struct {
			Contract string `json:"contract"`
			Bids     []struct {
				P string  `json:"p"`
				S float64 `json:"s"`
			} `json:"bids"`
			Asks []struct {
				P string  `json:"p"`
				S float64 `json:"s"`
			} `json:"asks"`
		} `json:"result"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Channel != "futures.order_book" {
		return nil, nil
	}
	if msg.Event != "all" && msg.Event != "update" {
		return nil, nil
	}
	contract := msg.Result.Contract
	if !strings.HasSuffix(contract, "_USDT") {
		return nil, nil
	}
	token := strings.TrimSuffix(contract, "_USDT")

	bk, ok := a.books[token]
	if !ok {
		bk = &book{bids: make(map[float64]float64), asks: make(map[float64]float64)}
		a.books[token] = bk
	}
	if msg.Event == "all" {
		bk.bids = make(map[float64]float64, len(msg.Result.Bids))
		bk.asks = make(map[float64]float64, len(msg.Result.Asks))
	}
	for _, lvl := range msg.Result.Bids {
		px, _ := strconv.ParseFloat(lvl.P, 64)
		if lvl.S == 0 {
			delete(bk.bids, px)
		} else {
			bk.bids[px] = lvl.S
		}
	}
	for _, lvl := range msg.Result.Asks {
		px, _ := strconv.ParseFloat(lvl.P, 64)
		if lvl.S == 0 {
			delete(bk.asks, px)
		} else {
			bk.asks[px] = lvl.S
		}
	}
	return &ws.Snapshot{
		Symbol: token,
		Bids:   ws.SortedLevels(bk.bids, ws.Bids, 200),
		Asks:   ws.SortedLevels(bk.asks, ws.Asks, 200),
	}, nil
}

// Gate uses lib-level WS pings for keepalive. No app-level frame.
func (a *Futures) Heartbeat() []byte                { return nil }
func (a *Futures) HeartbeatInterval() time.Duration { return 0 }
func (a *Futures) PongFor(_ []byte) []byte          { return nil }
func (a *Futures) UseLibPings() bool                { return true }
func (a *Futures) SubscribeDelay() time.Duration    { return 0 }
func (a *Futures) MaxSymbols() int                  { return 0 }
func (a *Futures) DecompressGzip() bool             { return false }

func (a *Futures) OnReconnect() {
	a.books = make(map[string]*book)
}
