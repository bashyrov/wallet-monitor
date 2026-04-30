// Package hyperliquid — Hyperliquid L1 perp DEX, WS orderbook.
//
// URL: wss://api.hyperliquid.xyz/ws
// Subscribe: {"method":"subscribe","subscription":{"type":"l2Book","coin":"BTC"}}
//
// Inbound:
//   {"channel":"l2Book","data":{"coin":"BTC","time":...,"levels":[
//      [{"px":"60000","sz":"1.5","n":3}, ...],   // bids
//      [{"px":"60001","sz":"2.1","n":4}, ...]    // asks
//    ]}}
//
// QUIRKS:
//   - levels[0] = bids, levels[1] = asks
//   - Each level is an OBJECT with px/sz/n, NOT an array — separate parse
//     path from Binance/Bybit
package hyperliquid

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const futuresWS = "wss://api.hyperliquid.xyz/ws"

type Futures struct {
	store *cache.Store
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{store: store}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("hyperliquid", snap.Symbol, snap, "ws")
	})
}

func (a *Futures) Name() string                          { return "hyperliquid" }
func (a *Futures) URL(_ context.Context) (string, error) { return futuresWS, nil }

func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
	frames := make([][]byte, 0, len(symbols))
	for _, s := range symbols {
		f := map[string]any{
			"method":       "subscribe",
			"subscription": map[string]any{"type": "l2Book", "coin": strings.ToUpper(s)},
		}
		b, _ := ws.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
	var msg struct {
		Channel string `json:"channel"`
		Data    struct {
			Coin   string `json:"coin"`
			Levels [2][]struct {
				Px string  `json:"px"`
				Sz string  `json:"sz"`
				N  int     `json:"n"`
			} `json:"levels"`
		} `json:"data"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Channel != "l2Book" {
		return nil, nil
	}
	parseSide := func(rows []struct {
		Px string `json:"px"`
		Sz string `json:"sz"`
		N  int    `json:"n"`
	}) []ws.Level {
		out := make([]ws.Level, 0, len(rows))
		for _, r := range rows {
			px, _ := strconv.ParseFloat(r.Px, 64)
			sz, _ := strconv.ParseFloat(r.Sz, 64)
			if sz > 0 {
				out = append(out, ws.Level{px, sz})
			}
		}
		return out
	}
	return &ws.Snapshot{
		Symbol: strings.ToUpper(msg.Data.Coin),
		Bids:   parseSide(msg.Data.Levels[0]),
		Asks:   parseSide(msg.Data.Levels[1]),
	}, nil
}

// Hyperliquid uses lib WS pings.
func (a *Futures) Heartbeat() []byte                { return nil }
func (a *Futures) HeartbeatInterval() time.Duration { return 0 }
func (a *Futures) PongFor(_ []byte) []byte          { return nil }
func (a *Futures) UseLibPings() bool                { return true }
func (a *Futures) SubscribeDelay() time.Duration    { return 0 }
func (a *Futures) MaxSymbols() int                  { return 0 }
func (a *Futures) DecompressGzip() bool             { return false }
func (a *Futures) OnReconnect()                     {}
