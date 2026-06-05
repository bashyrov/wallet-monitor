// Package bingx — BingX USDT-perp orderbook.
//
// URL: wss://open-api-swap.bingx.com/swap-market
//
// Default channel: @depth20 (~100ms snapshot, 20 levels).
//   Subscribe: {"id":"X","reqType":"sub","dataType":"BTC-USDT@depth20"}
//
// BBO channel (BINGX_USE_BBO=1): @bookTicker — real-time top-of-book.
//   Subscribe: {"id":"X","reqType":"sub","dataType":"BTC-USDT@bookTicker"}
//   Inbound:   {"dataType":"BTC-USDT@bookTicker",
//               "data":{"b":"bidPx","B":"bidSz","a":"askPx","A":"askSz","T":N}}
//
// QUIRKS (PLAN bug #5):
//   - Frames are gzip-compressed → DecompressGzip() = true
//   - Server sends a literal "Ping" text every ~5s and CLOSES the
//     connection if we don't reply with literal "Pong" — so we hook
//     PongFor() to catch it.
//   - Lib-level WS pings are IGNORED → UseLibPings() = false.
package bingx

import (
	"bytes"
	"context"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const futuresWS = "wss://open-api-swap.bingx.com/swap-market"

type Futures struct {
	store  *cache.Store
	useBBO bool // BINGX_USE_BBO=1 → @bookTicker; false → @depth20
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{
		store:  store,
		useBBO: os.Getenv("BINGX_USE_BBO") == "1",
	}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("bingx", snap.Symbol, snap, "ws")
	})
}

func (a *Futures) Name() string                          { return "bingx" }
func (a *Futures) URL(_ context.Context) (string, error) { return futuresWS, nil }

func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
	suffix := "@depth20"
	if a.useBBO {
		suffix = "@bookTicker"
	}
	frames := make([][]byte, 0, len(symbols))
	for i, s := range symbols {
		f := map[string]any{
			"id":       strconv.Itoa(i + 1),
			"reqType":  "sub",
			"dataType": strings.ToUpper(s) + "-USDT" + suffix,
		}
		b, _ := ws.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
	// Detect which channel type this frame is for.
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

// parseBookTicker handles @bookTicker frames.
// Wire: {"dataType":"BTC-USDT@bookTicker","data":{"b":"px","B":"sz","a":"px","A":"sz","T":N}}
func (a *Futures) parseBookTicker(frame []byte) (*ws.Snapshot, error) {
	var msg struct {
		DataType string `json:"dataType"`
		Data     struct {
			B  string `json:"b"` // best bid price
			Bq string `json:"B"` // best bid qty
			A  string `json:"a"` // best ask price
			Aq string `json:"A"` // best ask qty
			T  int64  `json:"T"` // timestamp ms
		} `json:"data"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	pair := strings.SplitN(msg.DataType, "@", 2)[0] // "BTC-USDT"
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

	snap := &ws.Snapshot{
		Symbol: token,
		Bids:   []ws.Level{{bidPx, bidSz}},
		Asks:   []ws.Level{{askPx, askSz}},
	}
	if msg.Data.T > 0 {
		snap.EventTime = time.UnixMilli(msg.Data.T)
	}
	return snap, nil
}

// parseDepth handles @depth20 snapshot frames.
func (a *Futures) parseDepth(frame []byte) (*ws.Snapshot, error) {
	var msg struct {
		DataType string `json:"dataType"`
		Ts       int64  `json:"ts"` // envelope ms; some shapes carry it here
		Data     struct {
			Bids [][]string `json:"bids"`
			Asks [][]string `json:"asks"`
			Ts   int64      `json:"T"` // depth wire carries depth-snapshot ts in T
		} `json:"data"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	pair := strings.SplitN(msg.DataType, "@", 2)[0] // "BTC-USDT"
	if !strings.HasSuffix(pair, "-USDT") {
		return nil, nil
	}
	token := strings.TrimSuffix(pair, "-USDT")

	snap := &ws.Snapshot{Symbol: token}
	for _, r := range msg.Data.Bids {
		if len(r) < 2 {
			continue
		}
		px, _ := strconv.ParseFloat(r[0], 64)
		sz, _ := strconv.ParseFloat(r[1], 64)
		if sz > 0 {
			snap.Bids = append(snap.Bids, ws.Level{px, sz})
		}
	}
	for _, r := range msg.Data.Asks {
		if len(r) < 2 {
			continue
		}
		px, _ := strconv.ParseFloat(r[0], 64)
		sz, _ := strconv.ParseFloat(r[1], 64)
		if sz > 0 {
			snap.Asks = append(snap.Asks, ws.Level{px, sz})
		}
	}
	switch {
	case msg.Data.Ts > 0:
		snap.EventTime = time.UnixMilli(msg.Data.Ts)
	case msg.Ts > 0:
		snap.EventTime = time.UnixMilli(msg.Ts)
	}
	return snap, nil
}

// BingX server sends a gzipped "Ping" text frame every ~5s. The runner's
// gunzip step happens BEFORE PongFor — by the time we see the bytes here,
// they're plaintext. We accept both "Ping" and the JSON variant
// {"ping": "..."} which some upgraded BingX endpoints use.
func (a *Futures) PongFor(frame []byte) []byte {
	// Plain text "Ping"
	if bytes.Equal(bytes.TrimSpace(frame), []byte("Ping")) {
		return []byte("Pong")
	}
	// JSON ping
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
func (a *Futures) OnReconnect()                     {}
