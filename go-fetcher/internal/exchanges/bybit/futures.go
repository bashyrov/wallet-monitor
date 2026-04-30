// Package bybit implements the Bybit V5 perp orderbook WS.
//
// Channel: orderbook.50.{symbol}USDT — snapshot + delta protocol. First
// message has `type: "snapshot"`, subsequent are `type: "delta"`. We
// merge in place: zero-size deltas remove the level.
//
// URL: wss://stream.bybit.com/v5/public/linear
//
// Bug-resistance:
//   - Bug #1  (TEXT frame)        : runner.SendText only
//   - Bug #2  (policy 1008)       : runner backoff (Bybit not historically prone)
//   - Bug #7  (volume on partial) : DELTAS carry size only on changed levels;
//                                   our merge preserves untouched sizes.
//   - Bug #20 (stale TCP)         : runner watchdog
package bybit

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const futuresWS = "wss://stream.bybit.com/v5/public/linear"

type Futures struct {
	store *cache.Store
	// Per-symbol price→size for both sides. Bybit V5 sends snapshots on
	// connect + deltas after; we maintain the merged book here.
	books map[string]*book
}

type book struct {
	bids map[float64]float64
	asks map[float64]float64
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{store: store, books: make(map[string]*book)}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("bybit", snap.Symbol, snap, "ws")
	})
}

func (a *Futures) Name() string                    { return "bybit" }
func (a *Futures) URL(_ context.Context) (string, error) { return futuresWS, nil }

func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
	args := make([]string, len(symbols))
	for i, s := range symbols {
		args[i] = "orderbook.50." + strings.ToUpper(s) + "USDT"
	}
	frame := map[string]any{
		"op":   "subscribe",
		"args": args,
	}
	b, _ := ws.MarshalJSON(frame)
	return [][]byte{b}
}

func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
	// Bybit uses three top-level shapes: subscribe ack {success, op, ...},
	// pong {op:pong}, data {topic:"orderbook.50.X", type:"snapshot|delta", data:{...}}.
	var msg struct {
		Topic string `json:"topic"`
		Type  string `json:"type"`
		Data  struct {
			Symbol string     `json:"s"`
			Bids   [][]string `json:"b"`
			Asks   [][]string `json:"a"`
		} `json:"data"`
		Op  string `json:"op"`
		Ret string `json:"retMsg"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Op != "" || msg.Ret != "" {
		// subscribe ack / pong — not data
		return nil, nil
	}
	if !strings.HasPrefix(msg.Topic, "orderbook.50.") {
		return nil, nil
	}
	sym := msg.Data.Symbol
	if !strings.HasSuffix(sym, "USDT") {
		return nil, nil
	}
	token := strings.TrimSuffix(sym, "USDT")

	bk, ok := a.books[token]
	if !ok {
		bk = &book{bids: make(map[float64]float64), asks: make(map[float64]float64)}
		a.books[token] = bk
	}
	if msg.Type == "snapshot" {
		bk.bids = make(map[float64]float64, len(msg.Data.Bids))
		bk.asks = make(map[float64]float64, len(msg.Data.Asks))
	}
	apply := func(side map[float64]float64, rows [][]string) {
		for _, r := range rows {
			if len(r) < 2 {
				continue
			}
			px, perr := strconv.ParseFloat(r[0], 64)
			sz, serr := strconv.ParseFloat(r[1], 64)
			if perr != nil || serr != nil {
				continue
			}
			if sz == 0 {
				delete(side, px)
			} else {
				side[px] = sz
			}
		}
	}
	apply(bk.bids, msg.Data.Bids)
	apply(bk.asks, msg.Data.Asks)

	return &ws.Snapshot{
		Symbol: token,
		Bids:   ws.SortedLevels(bk.bids, ws.Bids, 200),
		Asks:   ws.SortedLevels(bk.asks, ws.Asks, 200),
	}, nil
}

// Bybit V5 keepalive — observed in prod (Singapore IP):
//   1. Client must send {"op":"ping"} every <30s, else server closes.
//   2. Server ALSO sends {"op":"ping"} unsolicited and expects
//      {"op":"pong"} reply within ~10s. Without reply, server closes.
// Both directions handled here: Heartbeat for client→server, PongFor
// for server→client. Bug found via wire trace — adapter parsed ack/pong
// but missed inbound ping, which silently killed the connection.
func (a *Futures) Heartbeat() []byte                { return []byte(`{"op":"ping"}`) }
func (a *Futures) HeartbeatInterval() time.Duration { return 20 * time.Second }
func (a *Futures) PongFor(frame []byte) []byte {
	// Quick prefix check before parse — bybit's data frames don't have
	// `"op":"ping"` so this filter avoids per-frame JSON parsing on the
	// hot path (300+ delta frames/s/symbol).
	if !bytesContainsOpPing(frame) {
		return nil
	}
	return []byte(`{"op":"pong"}`)
}
func (a *Futures) UseLibPings() bool { return false }

// bytesContainsOpPing — true if the frame contains the literal substring
// `"op":"ping"`. Cheap and good enough — false positives only occur if
// the substring shows up inside another payload (extremely unlikely
// given Bybit's JSON shapes).
func bytesContainsOpPing(b []byte) bool {
	const needle = `"op":"ping"`
	if len(b) < len(needle) {
		return false
	}
	for i := 0; i <= len(b)-len(needle); i++ {
		match := true
		for j := 0; j < len(needle); j++ {
			if b[i+j] != needle[j] {
				match = false
				break
			}
		}
		if match {
			return true
		}
	}
	return false
}
func (a *Futures) SubscribeDelay() time.Duration    { return 0 }
func (a *Futures) MaxSymbols() int                  { return 0 }
func (a *Futures) DecompressGzip() bool             { return false }

// OnReconnect — clear local books so the snapshot bleeds in cleanly.
func (a *Futures) OnReconnect() {
	a.books = make(map[string]*book)
}
