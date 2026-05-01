// HTX spot orderbook WS.
//
// URL: wss://api.huobi.pro/ws (different host from linear-swap-ws).
// Channel: market.<sym>usdt.depth.step0 — full snapshot pushes; symbol
// form is lowercase concatenated, no dash (vs futures `BTC-USDT`).
//
// Frame shape:
//   {"ch":"market.btcusdt.depth.step0","ts":...,
//    "tick":{"bids":[[px,sz],...],"asks":[[px,sz],...]}}
//
// Same gzip + JSON ping/pong as futures.
package htx

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const spotWS = "wss://api.huobi.pro/ws"

type Spot struct {
	store *cache.Store
	books map[string]*book
}

func NewSpot(store *cache.Store) *ws.Runner {
	a := &Spot{store: store, books: make(map[string]*book)}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("htx_spot", snap.Symbol, snap, "ws")
	})
}

func (a *Spot) Name() string                          { return "htx_spot" }
func (a *Spot) URL(_ context.Context) (string, error) { return spotWS, nil }

func (a *Spot) BuildSubscribe(symbols []string) [][]byte {
	frames := make([][]byte, 0, len(symbols))
	for i, s := range symbols {
		f := map[string]any{
			"sub": "market." + strings.ToLower(s) + "usdt.depth.step0",
			"id":  strconv.Itoa(i + 1),
		}
		b, _ := ws.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

func (a *Spot) Parse(frame []byte) (*ws.Snapshot, error) {
	var msg struct {
		Ch   string `json:"ch"`
		Tick struct {
			Bids [][]float64 `json:"bids"`
			Asks [][]float64 `json:"asks"`
		} `json:"tick"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if !strings.HasPrefix(msg.Ch, "market.") || !strings.Contains(msg.Ch, ".depth.") {
		return nil, nil
	}
	// "market.btcusdt.depth.step0" → "btcusdt"
	parts := strings.SplitN(msg.Ch, ".", 4)
	if len(parts) < 2 {
		return nil, nil
	}
	pair := strings.ToUpper(parts[1])
	if !strings.HasSuffix(pair, "USDT") {
		return nil, nil
	}
	token := strings.TrimSuffix(pair, "USDT")

	bk, ok := a.books[token]
	if !ok {
		bk = &book{bids: make(map[float64]float64), asks: make(map[float64]float64)}
		a.books[token] = bk
	}
	// step0 returns full top-N snapshots — replace each tick.
	bk.bids = make(map[float64]float64, len(msg.Tick.Bids))
	bk.asks = make(map[float64]float64, len(msg.Tick.Asks))
	for _, r := range msg.Tick.Bids {
		if len(r) >= 2 && r[1] > 0 {
			bk.bids[r[0]] = r[1]
		}
	}
	for _, r := range msg.Tick.Asks {
		if len(r) >= 2 && r[1] > 0 {
			bk.asks[r[0]] = r[1]
		}
	}
	return &ws.Snapshot{
		Symbol: token,
		Bids:   ws.SortedLevels(bk.bids, ws.Bids, 200),
		Asks:   ws.SortedLevels(bk.asks, ws.Asks, 200),
	}, nil
}

// HTX spot sends {"ping": N} every 5s — same protocol as futures.
func (a *Spot) PongFor(frame []byte) []byte {
	var msg struct {
		Ping int64 `json:"ping"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil
	}
	if msg.Ping == 0 {
		return nil
	}
	reply, _ := ws.MarshalJSON(map[string]int64{"pong": msg.Ping})
	return reply
}

func (a *Spot) Heartbeat() []byte                { return nil }
func (a *Spot) HeartbeatInterval() time.Duration { return 0 }
func (a *Spot) UseLibPings() bool                { return false }
func (a *Spot) SubscribeDelay() time.Duration    { return 0 }
func (a *Spot) MaxSymbols() int                  { return 0 }
func (a *Spot) DecompressGzip() bool             { return true }
func (a *Spot) OnReconnect()                     { a.books = make(map[string]*book) }
