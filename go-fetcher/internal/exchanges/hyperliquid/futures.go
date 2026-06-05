// Package hyperliquid — Hyperliquid L1 perp DEX, WS orderbook.
//
// URL: wss://api.hyperliquid.xyz/ws
//
// Default channel: l2Book — snapshot per block update, ≥500ms cadence.
//   Subscribe: {"method":"subscribe","subscription":{"type":"l2Book","coin":"BTC"}}
//   Inbound:   {"channel":"l2Book","data":{"coin":"BTC","time":N,
//               "levels":[[{px,sz,n},...],[{px,sz,n},...]]}}
//
// BBO channel (HL_USE_BBO=1): hybrid dual-track:
//   - l2Book subscribed → feeds books[coin] (full depth snapshot, per-block)
//   - bbo    subscribed → feeds bbo[coin]   (BBO overlay, per-block on change)
//   mergedSnapshot splices BBO over l2Book top → full ladder + fast BBO.
//   Note: both channels are block-bound so speed gain from BBO is marginal;
//   the primary benefit is that bbo fires immediately on any BBO change while
//   l2Book fires when the full snapshot is ready.
//
// QUIRKS:
//   - levels[0] = bids, levels[1] = asks
//   - Each level is an OBJECT with px/sz/n, NOT an array
//   - HL drops connection with "write: broken pipe" after 4-8 subscribe
//     frames — 500ms SubscribeDelay is required
package hyperliquid

import (
	"context"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const futuresWS = "wss://api.hyperliquid.xyz/ws"

type l2Snap struct {
	bids []ws.Level
	asks []ws.Level
}

type bboLevel struct {
	bidPx, bidSz float64
	askPx, askSz float64
}

type Futures struct {
	store  *cache.Store
	useBBO bool // HL_USE_BBO=1 → dual-track (l2Book + bbo); false → l2Book only

	mu    sync.Mutex
	books map[string]*l2Snap
	bbo   map[string]*bboLevel
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{
		store:  store,
		useBBO: os.Getenv("HL_USE_BBO") == "1",
		books:  make(map[string]*l2Snap),
		bbo:    make(map[string]*bboLevel),
	}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("hyperliquid", snap.Symbol, snap, "ws")
	})
}

func (a *Futures) Name() string                          { return "hyperliquid" }
func (a *Futures) URL(_ context.Context) (string, error) { return futuresWS, nil }

func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
	// Dual-track when HL_USE_BBO=1: subscribe to BOTH l2Book AND bbo.
	// HL sends both channels per-block so the combined depth is coherent.
	chanTypes := []string{"l2Book"}
	if a.useBBO {
		chanTypes = append(chanTypes, "bbo")
	}
	frames := make([][]byte, 0, len(symbols)*len(chanTypes))
	for _, ct := range chanTypes {
		for _, s := range symbols {
			f := map[string]any{
				"method":       "subscribe",
				"subscription": map[string]any{"type": ct, "coin": strings.ToUpper(s)},
			}
			b, _ := ws.MarshalJSON(f)
			frames = append(frames, b)
		}
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
			Coin   string       `json:"coin"`
			Time   int64        `json:"time"`
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
		a.mu.Lock()
		b, ok := a.bbo[coin]
		if !ok {
			b = &bboLevel{}
			a.bbo[coin] = b
		}
		b.bidPx, b.bidSz = bidPx, bidSz
		b.askPx, b.askSz = askPx, askSz
		snap := a.mergedSnapshotLocked(coin)
		a.mu.Unlock()
		snap.EventTime = evt
		return snap, nil

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
		a.mu.Lock()
		a.books[coin] = &l2Snap{
			bids: parseSide(msg.Data.Levels[0]),
			asks: parseSide(msg.Data.Levels[1]),
		}
		snap := a.mergedSnapshotLocked(coin)
		a.mu.Unlock()
		snap.EventTime = evt
		return snap, nil

	default:
		return nil, nil
	}
}

// mergedSnapshotLocked — must hold mu. l2Book depth with BBO spliced on top.
func (a *Futures) mergedSnapshotLocked(coin string) *ws.Snapshot {
	var bids, asks []ws.Level
	if d := a.books[coin]; d != nil {
		bids = append([]ws.Level(nil), d.bids...)
		asks = append([]ws.Level(nil), d.asks...)
	}
	if b := a.bbo[coin]; b != nil {
		bids = spliceBBOBid(bids, b.bidPx, b.bidSz)
		asks = spliceBBOAsk(asks, b.askPx, b.askSz)
	}
	return &ws.Snapshot{Symbol: coin, Bids: bids, Asks: asks}
}

func spliceBBOBid(bids []ws.Level, bboPx, bboSz float64) []ws.Level {
	if bboPx <= 0 {
		return bids
	}
	if len(bids) == 0 {
		return []ws.Level{{bboPx, bboSz}}
	}
	if bboPx > bids[0][0] {
		return append([]ws.Level{{bboPx, bboSz}}, bids...)
	}
	if bboPx == bids[0][0] {
		bids[0][1] = bboSz
	}
	return bids
}

func spliceBBOAsk(asks []ws.Level, bboPx, bboSz float64) []ws.Level {
	if bboPx <= 0 {
		return asks
	}
	if len(asks) == 0 {
		return []ws.Level{{bboPx, bboSz}}
	}
	if bboPx < asks[0][0] {
		return append([]ws.Level{{bboPx, bboSz}}, asks...)
	}
	if bboPx == asks[0][0] {
		asks[0][1] = bboSz
	}
	return asks
}

// Hyperliquid keepalive — server sends ping frames, gorilla auto-replies.
func (a *Futures) Heartbeat() []byte                { return nil }
func (a *Futures) HeartbeatInterval() time.Duration { return 0 }
func (a *Futures) PongFor(_ []byte) []byte          { return nil }
func (a *Futures) UseLibPings() bool                { return true }

// HL drops connection with "write: broken pipe" after 4-8 subscribe frames.
// 500ms gap keeps us at 2 subs/s.
func (a *Futures) SubscribeDelay() time.Duration { return 500 * time.Millisecond }
func (a *Futures) MaxSymbols() int               { return 0 }
func (a *Futures) DecompressGzip() bool          { return false }
func (a *Futures) OnReconnect() {
	a.mu.Lock()
	a.books = make(map[string]*l2Snap)
	a.bbo = make(map[string]*bboLevel)
	a.mu.Unlock()
}
