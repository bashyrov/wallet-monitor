// Package whitebit — WhiteBIT perp orderbook.
//
// URL: wss://api.whitebit.com/ws
// Subscribe (depth): ["depth_subscribe", ["BTC_PERP", 100, "0", true]]
//                                         market    limit  prec snap
//
// Inbound:
//   request shape: {"id": N, "result":..., "error": null} — ack
//   data shape:    {"method":"depth_update","params":[<bool isFull>,<{bids,asks}>,<market>], "id":null}
package whitebit

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const futuresWS = "wss://api.whitebit.com/ws"

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
		store.Store("whitebit", snap.Symbol, snap, "ws")
	})
}

func (a *Futures) Name() string                          { return "whitebit" }
func (a *Futures) URL(_ context.Context) (string, error) { return futuresWS, nil }

func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
	frames := make([][]byte, 0, len(symbols))
	for i, s := range symbols {
		params := []any{strings.ToUpper(s) + "_PERP", 100, "0", true}
		f := map[string]any{
			"id":     i + 1,
			"method": "depth_subscribe",
			"params": params,
		}
		b, _ := ws.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

// BuildUnsubscribe — sending depth_unsubscribe keeps the WS alive on
// symbol-set churn (Manager rotates prewarm every 5s). Without this the
// runner force-closes on every reconcile cycle that drops a symbol,
// killing in-flight orderbook updates → visible "stale" feel on /arb.
func (a *Futures) BuildUnsubscribe(symbols []string) [][]byte {
	if len(symbols) == 0 {
		return nil
	}
	markets := make([]any, 0, len(symbols))
	for _, s := range symbols {
		markets = append(markets, strings.ToUpper(s)+"_PERP")
	}
	f := map[string]any{
		"id":     1,
		"method": "depth_unsubscribe",
		"params": markets,
	}
	b, _ := ws.MarshalJSON(f)
	// Clear local diff-state for removed markets so a future re-subscribe
	// gets a clean book.
	for _, s := range symbols {
		delete(a.books, strings.ToUpper(s))
	}
	return [][]byte{b}
}

func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
	var msg struct {
		Method string `json:"method"`
		Params []any  `json:"params"`
		ID     *int   `json:"id"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Method != "depth_update" {
		return nil, nil
	}
	if len(msg.Params) < 3 {
		return nil, nil
	}
	isFull, _ := msg.Params[0].(bool)
	body, ok := msg.Params[1].(map[string]any)
	if !ok {
		return nil, nil
	}
	market, _ := msg.Params[2].(string)
	if !strings.HasSuffix(market, "_PERP") {
		return nil, nil
	}
	token := strings.TrimSuffix(market, "_PERP")

	bk, ok := a.books[token]
	if !ok {
		bk = &book{bids: make(map[float64]float64), asks: make(map[float64]float64)}
		a.books[token] = bk
	}
	if isFull {
		bk.bids = make(map[float64]float64)
		bk.asks = make(map[float64]float64)
	}
	apply := func(side map[float64]float64, key string) {
		raw, ok := body[key].([]any)
		if !ok {
			return
		}
		for _, lvl := range raw {
			pair, ok := lvl.([]any)
			if !ok || len(pair) < 2 {
				continue
			}
			pxStr, _ := pair[0].(string)
			szStr, _ := pair[1].(string)
			px, perr := strconv.ParseFloat(pxStr, 64)
			sz, serr := strconv.ParseFloat(szStr, 64)
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
	apply(bk.bids, "bids")
	apply(bk.asks, "asks")

	// WhiteBIT `timestamp` is fractional seconds since epoch; convert to ms.
	var evt time.Time
	if ts, ok := body["timestamp"].(float64); ok && ts > 0 {
		evt = time.UnixMilli(int64(ts * 1000))
	}
	return &ws.Snapshot{
		Symbol:    token,
		Bids:      ws.SortedLevels(bk.bids, ws.Bids, 200),
		Asks:      ws.SortedLevels(bk.asks, ws.Asks, 200),
		EventTime: evt,
	}, nil
}

// WhiteBIT supports lib-level WS pings.
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
