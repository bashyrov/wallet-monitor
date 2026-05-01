// Package bingx — BingX USDT-perp orderbook.
//
// URL: wss://open-api-swap.bingx.com/swap-market
// Subscribe: {"id":"X","reqType":"sub","dataType":"BTC-USDT@depth20"}
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
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const futuresWS = "wss://open-api-swap.bingx.com/swap-market"

type Futures struct {
	store *cache.Store
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{store: store}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("bingx", snap.Symbol, snap, "ws")
	})
}

func (a *Futures) Name() string                          { return "bingx" }
func (a *Futures) URL(_ context.Context) (string, error) { return futuresWS, nil }

func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
	frames := make([][]byte, 0, len(symbols))
	for i, s := range symbols {
		f := map[string]any{
			"id":       strconv.Itoa(i + 1),
			"reqType":  "sub",
			"dataType": strings.ToUpper(s) + "-USDT@depth20",
		}
		b, _ := ws.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
	var msg struct {
		DataType string `json:"dataType"`
		Data     struct {
			Bids [][]string `json:"bids"`
			Asks [][]string `json:"asks"`
		} `json:"data"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if !strings.Contains(msg.DataType, "@depth") {
		return nil, nil
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
