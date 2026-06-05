// Package hyperliquid — Hyperliquid L1 perp DEX, WS orderbook.
//
// URL: wss://api.hyperliquid.xyz/ws
//
// Default channel: l2Book — snapshot per block update, ≥500ms cadence.
//   Subscribe: {"method":"subscribe","subscription":{"type":"l2Book","coin":"BTC"}}
//   Inbound:   {"channel":"l2Book","data":{"coin":"BTC","time":N,
//               "levels":[[{px,sz,n},...],[{px,sz,n},...]]}}
//
// BBO channel (HL_USE_BBO=1): bbo — pushed on every BBO change per block.
//   Subscribe: {"method":"subscribe","subscription":{"type":"bbo","coin":"BTC"}}
//   Inbound:   {"channel":"bbo","data":{"coin":"BTC","time":N,
//               "bbo":[{px,sz,n},{px,sz,n}]}}
//   where bbo[0] = best bid, bbo[1] = best ask (same level shape as l2Book).
//
// QUIRKS:
//   - levels[0] = bids, levels[1] = asks
//   - Each level is an OBJECT with px/sz/n, NOT an array — separate parse
//     path from Binance/Bybit
//   - HL drops connection with "write: broken pipe" after 4-8 subscribe
//     frames in succession — 500ms SubscribeDelay is required
package hyperliquid

import (
	"context"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const futuresWS = "wss://api.hyperliquid.xyz/ws"

type Futures struct {
	store  *cache.Store
	useBBO bool // HL_USE_BBO=1 → bbo channel; false → l2Book
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{
		store:  store,
		useBBO: os.Getenv("HL_USE_BBO") == "1",
	}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("hyperliquid", snap.Symbol, snap, "ws")
	})
}

func (a *Futures) Name() string                          { return "hyperliquid" }
func (a *Futures) URL(_ context.Context) (string, error) { return futuresWS, nil }

func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
	chanType := "l2Book"
	if a.useBBO {
		chanType = "bbo"
	}
	frames := make([][]byte, 0, len(symbols))
	for _, s := range symbols {
		f := map[string]any{
			"method":       "subscribe",
			"subscription": map[string]any{"type": chanType, "coin": strings.ToUpper(s)},
		}
		b, _ := ws.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

// hlLevel matches both l2Book levels and bbo elements.
type hlLevel struct {
	Px string `json:"px"`
	Sz string `json:"sz"`
	N  int    `json:"n"`
}

func parseHLLevel(r hlLevel) (px, sz float64) {
	px, _ = strconv.ParseFloat(r.Px, 64)
	sz, _ = strconv.ParseFloat(r.Sz, 64)
	return
}

func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
	var msg struct {
		Channel string `json:"channel"`
		Data    struct {
			Coin   string    `json:"coin"`
			Time   int64     `json:"time"` // ms event time
			Levels [2][]hlLevel `json:"levels"` // l2Book: [bids, asks]
			BBO    [2]hlLevel   `json:"bbo"`    // bbo: [bid, ask]
		} `json:"data"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}

	var evt time.Time
	if msg.Data.Time > 0 {
		evt = time.UnixMilli(msg.Data.Time)
	}
	coin := strings.ToUpper(msg.Data.Coin)

	switch msg.Channel {
	case "bbo":
		bid := msg.Data.BBO[0]
		ask := msg.Data.BBO[1]
		bidPx, bidSz := parseHLLevel(bid)
		askPx, askSz := parseHLLevel(ask)
		if bidPx <= 0 || askPx <= 0 {
			return nil, nil
		}
		return &ws.Snapshot{
			Symbol:    coin,
			Bids:      []ws.Level{{bidPx, bidSz}},
			Asks:      []ws.Level{{askPx, askSz}},
			EventTime: evt,
		}, nil

	case "l2Book":
		parseSide := func(rows []hlLevel) []ws.Level {
			out := make([]ws.Level, 0, len(rows))
			for _, r := range rows {
				px, sz := parseHLLevel(r)
				if sz > 0 {
					out = append(out, ws.Level{px, sz})
				}
			}
			return out
		}
		return &ws.Snapshot{
			Symbol:    coin,
			Bids:      parseSide(msg.Data.Levels[0]),
			Asks:      parseSide(msg.Data.Levels[1]),
			EventTime: evt,
		}, nil

	default:
		return nil, nil
	}
}

// Hyperliquid keepalive — server sends ping frames, gorilla auto-replies
// with WS-frame pong. No app-level heartbeat required.
func (a *Futures) Heartbeat() []byte                { return nil }
func (a *Futures) HeartbeatInterval() time.Duration { return 0 }
func (a *Futures) PongFor(_ []byte) []byte          { return nil }
func (a *Futures) UseLibPings() bool                { return true }

// HL drops the connection with "write: broken pipe" after 4-8 subscribe
// frames in succession — confirmed in prod logs after the 100ms test.
// 500ms gives us 2 subs/s, ~10s subscribe phase for 20 symbols. Subs
// happen once per connection so the long phase is acceptable.
func (a *Futures) SubscribeDelay() time.Duration { return 500 * time.Millisecond }
func (a *Futures) MaxSymbols() int               { return 0 }
func (a *Futures) DecompressGzip() bool          { return false }
func (a *Futures) OnReconnect()                  {}
