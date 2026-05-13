// Package kraken — Kraken Futures (linear perp).
//
// URL: wss://futures.kraken.com/ws/v1
// Subscribe: {"event":"subscribe","feed":"book","product_ids":["PF_BTCUSD","PF_ETHUSD",...]}
//
// QUIRKS:
//   - Symbol form: PF_<TOKEN>USD with XBT alias for BTC
//     (BTC token → product_id PF_XBTUSD)
//   - Bids returned ASCENDING (worst-first); we reverse for caller-side
//     "best-first" expectation.
//   - Snapshot frame has feed="book_snapshot"; deltas have feed="book".
package kraken

import (
	"context"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const futuresWS = "wss://futures.kraken.com/ws/v1"

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
		store.Store("kraken", snap.Symbol, snap, "ws")
	})
}

func (a *Futures) Name() string                          { return "kraken" }
func (a *Futures) URL(_ context.Context) (string, error) { return futuresWS, nil }

func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
	products := make([]string, len(symbols))
	for i, s := range symbols {
		token := strings.ToUpper(s)
		if token == "BTC" {
			token = "XBT"
		}
		products[i] = "PF_" + token + "USD"
	}
	frame := map[string]any{
		"event":       "subscribe",
		"feed":        "book",
		"product_ids": products,
	}
	b, _ := ws.MarshalJSON(frame)
	return [][]byte{b}
}

// Kraken pushes two distinct frame shapes:
//
//	feed=book_snapshot:  full snapshot with bids/asks arrays of objects
//	                     {price, qty}
//	feed=book:           single-level update {side: "buy"|"sell", price, qty}
//
// Both share product_id.
func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
	var msg struct {
		Feed      string `json:"feed"`
		Event     string `json:"event"`
		ProductID string `json:"product_id"`
		Side      string `json:"side"`
		Price     float64 `json:"price"`
		Qty       float64 `json:"qty"`
		Timestamp int64   `json:"timestamp"` // ms-since-epoch
		Bids      []struct {
			Price float64 `json:"price"`
			Qty   float64 `json:"qty"`
		} `json:"bids"`
		Asks []struct {
			Price float64 `json:"price"`
			Qty   float64 `json:"qty"`
		} `json:"asks"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	// info / subscribed events — not data
	if msg.Event != "" {
		return nil, nil
	}
	pid := msg.ProductID
	if !strings.HasPrefix(pid, "PF_") || !strings.HasSuffix(pid, "USD") {
		return nil, nil
	}
	token := strings.TrimSuffix(strings.TrimPrefix(pid, "PF_"), "USD")
	if token == "XBT" {
		token = "BTC"
	}

	bk, ok := a.books[token]
	if !ok {
		bk = &book{bids: make(map[float64]float64), asks: make(map[float64]float64)}
		a.books[token] = bk
	}

	switch msg.Feed {
	case "book_snapshot":
		bk.bids = make(map[float64]float64, len(msg.Bids))
		bk.asks = make(map[float64]float64, len(msg.Asks))
		for _, b := range msg.Bids {
			if b.Qty > 0 {
				bk.bids[b.Price] = b.Qty
			}
		}
		for _, a := range msg.Asks {
			if a.Qty > 0 {
				bk.asks[a.Price] = a.Qty
			}
		}
	case "book":
		// per-level delta
		var side map[float64]float64
		switch msg.Side {
		case "buy":
			side = bk.bids
		case "sell":
			side = bk.asks
		default:
			return nil, nil
		}
		if msg.Qty == 0 {
			delete(side, msg.Price)
		} else {
			side[msg.Price] = msg.Qty
		}
	default:
		return nil, nil
	}

	var evt time.Time
	if msg.Timestamp > 0 {
		evt = time.UnixMilli(msg.Timestamp)
	}
	return &ws.Snapshot{
		Symbol:    token,
		Bids:      ws.SortedLevels(bk.bids, ws.Bids, 200),
		Asks:      ws.SortedLevels(bk.asks, ws.Asks, 200),
		EventTime: evt,
	}, nil
}

// Kraken WS supports lib pings.
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
