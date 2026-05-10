// Package lighter — Lighter zkPerp DEX WS orderbook.
//
// URL: wss://mainnet.zklighter.elliot.ai/stream
// Subscribe: {"type":"subscribe","channel":"order_book/<market_id>"}
//
// QUIRK — snapshot vs delta: Lighter pushes two distinct frame types on
// the same channel:
//   - type="subscribed/order_book/N" → full snapshot, replace book
//   - type="update/order_book/N"     → delta, merge (size=0 deletes)
//
// Original parser treated every push as a snapshot, so a delta of 2-3
// levels would wipe out the full book. Result: 23 of 25 cached symbols
// had <10 levels in the depth audit. Now we distinguish via the Type
// field and maintain a local book per market_id.
package lighter

import (
	"context"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const futuresWS = "wss://mainnet.zklighter.elliot.ai/stream"

type Futures struct {
	store *cache.Store
	ids   *idMap
	mu    sync.Mutex
	books map[int]*book
}

type book struct {
	bids map[float64]float64
	asks map[float64]float64
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{
		store: store,
		ids:   newIDMap(),
		books: make(map[int]*book),
	}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("lighter", snap.Symbol, snap, "ws")
	})
}

func (a *Futures) Name() string { return "lighter" }

// URL — pre-resolves the id map so BuildSubscribe can convert symbols.
// Returns the static WS URL; the resolve work is a side-effect on the
// internal map.
func (a *Futures) URL(ctx context.Context) (string, error) {
	// best-effort warm — id map auto-refreshes inside Resolve() if cold.
	return futuresWS, nil
}

func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
	frames := make([][]byte, 0, len(symbols))
	ctx, cancel := context.WithTimeout(context.Background(), 6*time.Second)
	defer cancel()
	for _, s := range symbols {
		id, err := a.ids.Resolve(ctx, s)
		if err != nil {
			continue // symbol not on Lighter — skip silently
		}
		f := map[string]any{
			"type":    "subscribe",
			"channel": "order_book/" + strconv.Itoa(id),
		}
		b, _ := ws.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
	var msg struct {
		Type      string `json:"type"`
		Channel   string `json:"channel"`
		OrderBook struct {
			Asks []struct {
				Price string `json:"price"`
				Size  string `json:"size"`
			} `json:"asks"`
			Bids []struct {
				Price string `json:"price"`
				Size  string `json:"size"`
			} `json:"bids"`
		} `json:"order_book"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	// Lighter accepts subscribe channels as "order_book/<id>" but echoes
	// them back as "order_book:<id>" in the data stream — accept both.
	const prefSlash = "order_book/"
	const prefColon = "order_book:"
	var idStr string
	switch {
	case strings.HasPrefix(msg.Channel, prefSlash):
		idStr = strings.TrimPrefix(msg.Channel, prefSlash)
	case strings.HasPrefix(msg.Channel, prefColon):
		idStr = strings.TrimPrefix(msg.Channel, prefColon)
	default:
		return nil, nil
	}
	id, err := strconv.Atoi(idStr)
	if err != nil {
		return nil, nil
	}
	sym := a.ids.Symbol(id)
	if sym == "" {
		return nil, nil
	}

	// Distinguish snapshot vs delta. Lighter sets:
	//   "subscribed/order_book/N" → initial full snapshot
	//   "update/order_book/N"     → delta (only changed levels)
	// On snapshot we replace the book wholesale; on delta we merge level
	// by level (size=0 deletes). Treating delta as snapshot was the
	// shrinkage bug — a 2-level delta would wipe a 30-level book.
	isSnapshot := strings.HasPrefix(msg.Type, "subscribed/")

	a.mu.Lock()
	defer a.mu.Unlock()

	bk, ok := a.books[id]
	if !ok {
		bk = &book{bids: make(map[float64]float64), asks: make(map[float64]float64)}
		a.books[id] = bk
	}
	if isSnapshot {
		bk.bids = make(map[float64]float64, len(msg.OrderBook.Bids))
		bk.asks = make(map[float64]float64, len(msg.OrderBook.Asks))
	}
	apply := func(side map[float64]float64, rows []struct {
		Price string `json:"price"`
		Size  string `json:"size"`
	}) {
		for _, r := range rows {
			px, _ := strconv.ParseFloat(r.Price, 64)
			sz, _ := strconv.ParseFloat(r.Size, 64)
			if sz == 0 {
				delete(side, px)
			} else {
				side[px] = sz
			}
		}
	}
	apply(bk.bids, msg.OrderBook.Bids)
	apply(bk.asks, msg.OrderBook.Asks)

	return &ws.Snapshot{
		Symbol: sym,
		Bids:   ws.SortedLevels(bk.bids, ws.Bids, 200),
		Asks:   ws.SortedLevels(bk.asks, ws.Asks, 200),
	}, nil
}

func (a *Futures) Heartbeat() []byte                { return nil }
func (a *Futures) HeartbeatInterval() time.Duration { return 0 }
func (a *Futures) PongFor(_ []byte) []byte          { return nil }
func (a *Futures) UseLibPings() bool                { return true }
func (a *Futures) SubscribeDelay() time.Duration    { return 0 }
func (a *Futures) MaxSymbols() int                  { return 0 }
func (a *Futures) DecompressGzip() bool             { return false }
func (a *Futures) OnReconnect() {
	a.mu.Lock()
	a.books = make(map[int]*book)
	a.mu.Unlock()
}
