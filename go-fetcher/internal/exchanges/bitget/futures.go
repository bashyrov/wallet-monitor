// Package bitget — Bitget V2 USDT-FUTURES + SPOT (one shared host).
//
// URL: wss://ws.bitget.com/v2/ws/public
// Subscribe (futures): {"op":"subscribe","args":[{"instType":"USDT-FUTURES","channel":"books","instId":"BTCUSDT"}]}
// Subscribe (spot):    {"op":"subscribe","args":[{"instType":"SPOT","channel":"books","instId":"BTCUSDT"}]}
//
// QUIRKS — every fix from today's prod debug session:
//   - Bug #1  (TEXT only): SendText enforced by runner
//   - Bug #4  (app-level "ping"): Heartbeat returns []byte("ping") every 25s
//   - Bug #6  (lib pings ignored): UseLibPings() returns false — proven
//                  today that lib WS-frame pings make the server silently
//                  drop the connection within 30s
//   - Bug #15 (instType differs spot/futures): two adapter types share a
//                  parser; constructor picks the value
package bitget

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const baseURL = "wss://ws.bitget.com/v2/ws/public"

// Adapter handles either futures (instType=USDT-FUTURES) or spot
// (instType=SPOT) depending on which constructor was used.
type Adapter struct {
	store    *cache.Store
	cacheKey string // "bitget" or "bitget_spot"
	instType string // "USDT-FUTURES" or "SPOT"
	books    map[string]*book
}

type book struct {
	bids map[float64]float64
	asks map[float64]float64
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Adapter{
		store:    store,
		cacheKey: "bitget",
		instType: "USDT-FUTURES",
		books:    make(map[string]*book),
	}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("bitget", snap.Symbol, snap, "ws")
	})
}

func NewSpot(store *cache.Store) *ws.Runner {
	a := &Adapter{
		store:    store,
		cacheKey: "bitget_spot",
		instType: "SPOT",
		books:    make(map[string]*book),
	}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("bitget_spot", snap.Symbol, snap, "ws")
	})
}

func (a *Adapter) Name() string                          { return a.cacheKey }
func (a *Adapter) URL(_ context.Context) (string, error) { return baseURL, nil }

func (a *Adapter) BuildSubscribe(symbols []string) [][]byte {
	args := make([]map[string]string, len(symbols))
	for i, s := range symbols {
		args[i] = map[string]string{
			"instType": a.instType,
			"channel":  "books",
			"instId":   strings.ToUpper(s) + "USDT",
		}
	}
	frame := map[string]any{"op": "subscribe", "args": args}
	b, _ := ws.MarshalJSON(frame)
	return [][]byte{b}
}

func (a *Adapter) Parse(frame []byte) (*ws.Snapshot, error) {
	// Subscribe ack: {"event":"subscribe","arg":{...}}
	// Error event:   {"event":"error","msg":"...","code":...}
	// Data:          {"action":"snapshot|update","arg":{...},"data":[{"asks":[...],"bids":[...],"ts":...,"checksum":...}]}
	var msg struct {
		Event  string `json:"event"`
		Action string `json:"action"`
		Arg    struct {
			InstType string `json:"instType"`
			Channel  string `json:"channel"`
			InstID   string `json:"instId"`
		} `json:"arg"`
		Data []struct {
			Bids [][]string `json:"bids"`
			Asks [][]string `json:"asks"`
		} `json:"data"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Event != "" {
		return nil, nil
	}
	if msg.Arg.Channel != "books" {
		return nil, nil
	}
	if msg.Arg.InstType != a.instType {
		// Wrong leg — futures adapter shouldn't process spot data even if
		// the same connection ever multiplexed (it doesn't, but defensive).
		return nil, nil
	}
	if !strings.HasSuffix(msg.Arg.InstID, "USDT") {
		return nil, nil
	}
	token := strings.TrimSuffix(msg.Arg.InstID, "USDT")
	if len(msg.Data) == 0 {
		return nil, nil
	}
	d := msg.Data[0]

	bk, ok := a.books[token]
	if !ok {
		bk = &book{bids: make(map[float64]float64), asks: make(map[float64]float64)}
		a.books[token] = bk
	}
	if msg.Action == "snapshot" {
		bk.bids = make(map[float64]float64, len(d.Bids))
		bk.asks = make(map[float64]float64, len(d.Asks))
	}
	apply := func(side map[float64]float64, rows [][]string) {
		for _, r := range rows {
			if len(r) < 2 {
				continue
			}
			px, _ := strconv.ParseFloat(r[0], 64)
			sz, _ := strconv.ParseFloat(r[1], 64)
			if sz == 0 {
				delete(side, px)
			} else {
				side[px] = sz
			}
		}
	}
	apply(bk.bids, d.Bids)
	apply(bk.asks, d.Asks)
	return &ws.Snapshot{
		Symbol: token,
		Bids:   ws.SortedLevels(bk.bids, ws.Bids, 200),
		Asks:   ws.SortedLevels(bk.asks, ws.Asks, 200),
	}, nil
}

// Heartbeat — Bug #4 from today: Bitget V2 needs literal "ping" text frame
// every <30s. Server ignores lib pings (Bug #6). 25s gives margin.
func (a *Adapter) Heartbeat() []byte                { return []byte("ping") }
func (a *Adapter) HeartbeatInterval() time.Duration { return 25 * time.Second }

// Server replies with literal "pong" — runner consumes via the lowercase
// "ping"/"pong" path before reaching adapter Parse(). Nothing to do here.
func (a *Adapter) PongFor(_ []byte) []byte       { return nil }
func (a *Adapter) UseLibPings() bool              { return false } // bug #6
func (a *Adapter) SubscribeDelay() time.Duration { return 0 }
func (a *Adapter) MaxSymbols() int                { return 0 }
func (a *Adapter) DecompressGzip() bool           { return false }

func (a *Adapter) OnReconnect() {
	a.books = make(map[string]*book)
}
