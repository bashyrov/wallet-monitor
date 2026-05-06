// Package mexc — MEXC contract (USDT-margined linear perp).
//
// URL: wss://contract.mexc.com/edge
// Subscribe: {"method":"sub.depth","param":{"symbol":"BTC_USDT","limit":20}}
//
// Inbound — incremental depth protocol:
//   First push after subscribe is a full snapshot; subsequent pushes are
//   deltas (only changed levels). Size=0 means remove the level.
//
//   {"channel":"push.depth","data":{"asks":[[px,sz,n],...],"bids":[...],"version":N},
//    "symbol":"BTC_USDT","ts":...}
//
// Heartbeat: {"method":"ping"} → server replies {"channel":"pong"}.
package mexc

import (
	"context"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const futuresWS = "wss://contract.mexc.com/edge"

type Futures struct {
	store *cache.Store
	books map[string]*book
}

type book struct {
	bids   map[float64]float64
	asks   map[float64]float64
	seeded bool // first push after subscribe treated as snapshot
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{store: store, books: make(map[string]*book)}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("mexc", snap.Symbol, snap, "ws")
	})
}

func (a *Futures) Name() string                          { return "mexc" }
func (a *Futures) URL(_ context.Context) (string, error) { return futuresWS, nil }

func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
	frames := make([][]byte, 0, len(symbols))
	for _, s := range symbols {
		f := map[string]any{
			"method": "sub.depth",
			"param":  map[string]any{"symbol": strings.ToUpper(s) + "_USDT", "limit": 20},
		}
		b, _ := ws.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
	var msg struct {
		Channel string `json:"channel"`
		Symbol  string `json:"symbol"`
		Data    struct {
			Bids [][]float64 `json:"bids"`
			Asks [][]float64 `json:"asks"`
		} `json:"data"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Channel != "push.depth" {
		return nil, nil
	}
	if !strings.HasSuffix(msg.Symbol, "_USDT") {
		return nil, nil
	}
	token := strings.TrimSuffix(msg.Symbol, "_USDT")

	bk, ok := a.books[token]
	if !ok {
		bk = &book{bids: make(map[float64]float64), asks: make(map[float64]float64)}
		a.books[token] = bk
	}

	if !bk.seeded {
		// First push after subscribe is a full snapshot — replace the book.
		bk.bids = make(map[float64]float64, len(msg.Data.Bids))
		bk.asks = make(map[float64]float64, len(msg.Data.Asks))
		bk.seeded = true
	}

	// Apply levels: sz=0 (index 1) means remove; sz>0 means add/update.
	for _, r := range msg.Data.Bids {
		if len(r) < 2 {
			continue
		}
		if r[1] == 0 {
			delete(bk.bids, r[0])
		} else {
			bk.bids[r[0]] = r[1]
		}
	}
	for _, r := range msg.Data.Asks {
		if len(r) < 2 {
			continue
		}
		if r[1] == 0 {
			delete(bk.asks, r[0])
		} else {
			bk.asks[r[0]] = r[1]
		}
	}

	return &ws.Snapshot{
		Symbol: token,
		Bids:   ws.SortedLevels(bk.bids, ws.Bids, 200),
		Asks:   ws.SortedLevels(bk.asks, ws.Asks, 200),
	}, nil
}

// MEXC contract requires {"method":"ping"} every ~20s.
func (a *Futures) Heartbeat() []byte                { return []byte(`{"method":"ping"}`) }
func (a *Futures) HeartbeatInterval() time.Duration { return 18 * time.Second }
func (a *Futures) PongFor(_ []byte) []byte          { return nil }
func (a *Futures) UseLibPings() bool                { return false }
func (a *Futures) SubscribeDelay() time.Duration    { return 0 }
func (a *Futures) MaxSymbols() int                  { return 0 }
func (a *Futures) DecompressGzip() bool             { return false }

func (a *Futures) OnReconnect() {
	// Clear all books — next push per symbol will be treated as a fresh snapshot.
	a.books = make(map[string]*book)
}
