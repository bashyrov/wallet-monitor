// Package lighter — Lighter zkPerp DEX WS orderbook.
//
// URL: wss://mainnet.zklighter.elliot.ai/stream
// Subscribe: {"type":"subscribe","channel":"order_book/<market_id>"}
//
// Inbound: {"type":"update/order_book/N","channel":"order_book/N",
//   "order_book":{"asks":[{"price":"...","size":"..."},...], "bids":[...]}}
package lighter

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const futuresWS = "wss://mainnet.zklighter.elliot.ai/stream"

type Futures struct {
	store *cache.Store
	ids   *idMap
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{store: store, ids: newIDMap()}
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

	parse := func(rows []struct {
		Price string `json:"price"`
		Size  string `json:"size"`
	}) []ws.Level {
		out := make([]ws.Level, 0, len(rows))
		for _, r := range rows {
			px, _ := strconv.ParseFloat(r.Price, 64)
			sz, _ := strconv.ParseFloat(r.Size, 64)
			if sz > 0 {
				out = append(out, ws.Level{px, sz})
			}
		}
		return out
	}
	return &ws.Snapshot{
		Symbol: sym,
		Bids:   parse(msg.OrderBook.Bids),
		Asks:   parse(msg.OrderBook.Asks),
	}, nil
}

func (a *Futures) Heartbeat() []byte                { return nil }
func (a *Futures) HeartbeatInterval() time.Duration { return 0 }
func (a *Futures) PongFor(_ []byte) []byte          { return nil }
func (a *Futures) UseLibPings() bool                { return true }
func (a *Futures) SubscribeDelay() time.Duration    { return 0 }
func (a *Futures) MaxSymbols() int                  { return 0 }
func (a *Futures) DecompressGzip() bool             { return false }
func (a *Futures) OnReconnect()                     {}
