// Package bingx — BingX USDT-perp orderbook.
//
// URL: wss://open-api-swap.bingx.com/swap-market
//
// Default channel: @depth20 (~100ms snapshot, 20 levels).
//
// BBO channel (BINGX_USE_BBO=1): hybrid dual-track:
//   - @depth20     subscribed → feeds books[token] (20-level depth state)
//   - @bookTicker  subscribed → feeds bbo[token]   (BBO overlay, event-driven)
//   mergedSnapshot splices BBO over depth top → full ladder + fast BBO.
//
// QUIRKS:
//   - Frames are gzip-compressed → DecompressGzip() = true
//   - Server sends literal "Ping" every ~5s; PongFor() replies "Pong".
//   - Lib WS pings are IGNORED → UseLibPings() = false.
package bingx

import (
	"bytes"
	"context"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const futuresWS = "wss://open-api-swap.bingx.com/swap-market"

type depthSnap struct {
	bids []ws.Level
	asks []ws.Level
}

type bboLevel struct {
	bidPx, bidSz float64
	askPx, askSz float64
}

type Futures struct {
	store  *cache.Store
	useBBO bool // BINGX_USE_BBO=1 → dual-track (depth + BBO); false → depth only

	mu    sync.Mutex
	books map[string]*depthSnap
	bbo   map[string]*bboLevel
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{
		store:  store,
		useBBO: os.Getenv("BINGX_USE_BBO") == "1",
		books:  make(map[string]*depthSnap),
		bbo:    make(map[string]*bboLevel),
	}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("bingx", snap.Symbol, snap, "ws")
	})
}

func (a *Futures) Name() string                          { return "bingx" }
func (a *Futures) URL(_ context.Context) (string, error) { return futuresWS, nil }

func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
	// Dual-track when BINGX_USE_BBO=1: subscribe to BOTH @depth20 AND @bookTicker.
	// BingX requires one frame per symbol per channel.
	suffixes := []string{"@depth20"}
	if a.useBBO {
		suffixes = append(suffixes, "@bookTicker")
	}
	frames := make([][]byte, 0, len(symbols)*len(suffixes))
	id := 0
	for _, suffix := range suffixes {
		for _, s := range symbols {
			id++
			f := map[string]any{
				"id":       strconv.Itoa(id),
				"reqType":  "sub",
				"dataType": strings.ToUpper(s) + "-USDT" + suffix,
			}
			b, _ := ws.MarshalJSON(f)
			frames = append(frames, b)
		}
	}
	return frames
}

func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
	var hdr struct {
		DataType string `json:"dataType"`
		Ts       int64  `json:"ts"`
	}
	if err := ws.UnmarshalJSON(frame, &hdr); err != nil {
		return nil, err
	}
	if strings.Contains(hdr.DataType, "@bookTicker") {
		return a.parseBookTicker(frame)
	}
	if strings.Contains(hdr.DataType, "@depth") {
		return a.parseDepth(frame)
	}
	return nil, nil
}

// parseBookTicker updates bbo state and emits a merged snapshot.
func (a *Futures) parseBookTicker(frame []byte) (*ws.Snapshot, error) {
	var msg struct {
		DataType string `json:"dataType"`
		Data     struct {
			B  string `json:"b"`
			Bq string `json:"B"`
			A  string `json:"a"`
			Aq string `json:"A"`
			T  int64  `json:"T"`
		} `json:"data"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	pair := strings.SplitN(msg.DataType, "@", 2)[0]
	if !strings.HasSuffix(pair, "-USDT") {
		return nil, nil
	}
	token := strings.TrimSuffix(pair, "-USDT")

	bidPx, _ := strconv.ParseFloat(msg.Data.B, 64)
	bidSz, _ := strconv.ParseFloat(msg.Data.Bq, 64)
	askPx, _ := strconv.ParseFloat(msg.Data.A, 64)
	askSz, _ := strconv.ParseFloat(msg.Data.Aq, 64)
	if bidPx <= 0 || askPx <= 0 {
		return nil, nil
	}

	a.mu.Lock()
	b, ok := a.bbo[token]
	if !ok {
		b = &bboLevel{}
		a.bbo[token] = b
	}
	b.bidPx, b.bidSz = bidPx, bidSz
	b.askPx, b.askSz = askPx, askSz
	snap := a.mergedSnapshotLocked(token)
	a.mu.Unlock()

	if msg.Data.T > 0 {
		snap.EventTime = time.UnixMilli(msg.Data.T)
	}
	return snap, nil
}

// parseDepth handles @depth20 snapshot frames — full-replaces books state.
func (a *Futures) parseDepth(frame []byte) (*ws.Snapshot, error) {
	var msg struct {
		DataType string `json:"dataType"`
		Ts       int64  `json:"ts"`
		Data     struct {
			Bids [][]string `json:"bids"`
			Asks [][]string `json:"asks"`
			Ts   int64      `json:"T"`
		} `json:"data"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	pair := strings.SplitN(msg.DataType, "@", 2)[0]
	if !strings.HasSuffix(pair, "-USDT") {
		return nil, nil
	}
	token := strings.TrimSuffix(pair, "-USDT")

	bids := make([]ws.Level, 0, len(msg.Data.Bids))
	for _, r := range msg.Data.Bids {
		if len(r) < 2 {
			continue
		}
		px, _ := strconv.ParseFloat(r[0], 64)
		sz, _ := strconv.ParseFloat(r[1], 64)
		if sz > 0 {
			bids = append(bids, ws.Level{px, sz})
		}
	}
	asks := make([]ws.Level, 0, len(msg.Data.Asks))
	for _, r := range msg.Data.Asks {
		if len(r) < 2 {
			continue
		}
		px, _ := strconv.ParseFloat(r[0], 64)
		sz, _ := strconv.ParseFloat(r[1], 64)
		if sz > 0 {
			asks = append(asks, ws.Level{px, sz})
		}
	}

	a.mu.Lock()
	a.books[token] = &depthSnap{bids: bids, asks: asks}
	snap := a.mergedSnapshotLocked(token)
	a.mu.Unlock()

	switch {
	case msg.Data.Ts > 0:
		snap.EventTime = time.UnixMilli(msg.Data.Ts)
	case msg.Ts > 0:
		snap.EventTime = time.UnixMilli(msg.Ts)
	}
	return snap, nil
}

// mergedSnapshotLocked — must hold mu. Depth state with BBO spliced on top.
func (a *Futures) mergedSnapshotLocked(token string) *ws.Snapshot {
	var bids, asks []ws.Level
	if d := a.books[token]; d != nil {
		bids = append([]ws.Level(nil), d.bids...)
		asks = append([]ws.Level(nil), d.asks...)
	}
	if b := a.bbo[token]; b != nil {
		bids = spliceBBOBid(bids, b.bidPx, b.bidSz)
		asks = spliceBBOAsk(asks, b.askPx, b.askSz)
	}
	return &ws.Snapshot{Symbol: token, Bids: bids, Asks: asks}
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

func (a *Futures) PongFor(frame []byte) []byte {
	if bytes.Equal(bytes.TrimSpace(frame), []byte("Ping")) {
		return []byte("Pong")
	}
	if bytes.Contains(frame, []byte(`"ping"`)) && !bytes.Contains(frame, []byte("dataType")) {
		return []byte(`{"pong":""}`)
	}
	return nil
}

func (a *Futures) Heartbeat() []byte                { return nil }
func (a *Futures) HeartbeatInterval() time.Duration { return 0 }
func (a *Futures) UseLibPings() bool                { return false }
func (a *Futures) SubscribeDelay() time.Duration    { return 0 }
func (a *Futures) MaxSymbols() int                  { return 100 } // BingX caps WS at ~100
func (a *Futures) DecompressGzip() bool             { return true }
func (a *Futures) OnReconnect() {
	a.mu.Lock()
	a.books = make(map[string]*depthSnap)
	a.bbo = make(map[string]*bboLevel)
	a.mu.Unlock()
}
