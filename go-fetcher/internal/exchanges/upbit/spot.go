// Upbit spot orderbook WS.
//
// URL: wss://api.upbit.com/websocket/v1 — public, no auth.
//
// Subscribe frame is a JSON ARRAY (not object):
//
//	[{"ticket":"<id>"},
//	 {"type":"orderbook","codes":["USDT-BTC","USDT-ETH"]},
//	 {"format":"DEFAULT"}]
//
// Quirk: each subscribe frame REPLACES the connection's entire
// subscription set — Upbit has no delta-add. The adapter keeps a
// cumulative code set and always emits the full union, so the runner's
// delta-subscribe (BuildSubscribe(added)) still converges. Removals
// fall back to the runner's reconnect path (no Unsubscriber).
//
// Push (DEFAULT format) arrives as a BINARY WS frame (JSON payload)
// with 30 levels per side, best-first, numbers as JSON floats:
//
//	{"type":"orderbook","code":"USDT-BTC","timestamp":...,
//	 "orderbook_units":[{"ask_price":..,"bid_price":..,"ask_size":..,"bid_size":..},...]}
//
// Keepalive: Upbit terminates idle connections after ~120s. Client text
// "PING" gets {"status":"UP"} back — sent every 50s, ignored in Parse.
package upbit

import (
	"context"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const spotWSURL = "wss://api.upbit.com/websocket/v1"

type Spot struct {
	mu   sync.Mutex
	subs map[string]struct{}
}

func NewSpot(store *cache.Store) *ws.Runner {
	a := &Spot{subs: make(map[string]struct{})}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("upbit_spot", snap.Symbol, snap, "ws")
	})
}

func (a *Spot) Name() string                          { return "upbit_spot" }
func (a *Spot) URL(_ context.Context) (string, error) { return spotWSURL, nil }

func (a *Spot) BuildSubscribe(symbols []string) [][]byte {
	a.mu.Lock()
	for _, s := range symbols {
		if s != "" {
			a.subs[strings.ToUpper(s)] = struct{}{}
		}
	}
	codes := make([]string, 0, len(a.subs))
	for s := range a.subs {
		codes = append(codes, "USDT-"+s)
	}
	a.mu.Unlock()
	if len(codes) == 0 {
		return nil
	}
	sort.Strings(codes)
	frame := []any{
		map[string]string{"ticket": "avalant-ob-" + strconv.FormatInt(time.Now().UnixNano(), 36)},
		map[string]any{"type": "orderbook", "codes": codes},
		map[string]string{"format": "DEFAULT"},
	}
	b, _ := ws.MarshalJSON(frame)
	return [][]byte{b}
}

func (a *Spot) Parse(frame []byte) (*ws.Snapshot, error) {
	var msg struct {
		Type      string `json:"type"`
		Code      string `json:"code"`
		Timestamp int64  `json:"timestamp"`
		Units     []struct {
			AskPrice float64 `json:"ask_price"`
			BidPrice float64 `json:"bid_price"`
			AskSize  float64 `json:"ask_size"`
			BidSize  float64 `json:"bid_size"`
		} `json:"orderbook_units"`
		Status string `json:"status"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Type != "orderbook" || len(msg.Units) == 0 {
		return nil, nil // PING reply {"status":"UP"} or non-book frame
	}
	token, ok := strings.CutPrefix(msg.Code, "USDT-")
	if !ok {
		return nil, nil
	}
	bids := make([]ws.Level, 0, len(msg.Units))
	asks := make([]ws.Level, 0, len(msg.Units))
	for _, u := range msg.Units {
		if u.BidPrice > 0 && u.BidSize > 0 {
			bids = append(bids, ws.Level{u.BidPrice, u.BidSize})
		}
		if u.AskPrice > 0 && u.AskSize > 0 {
			asks = append(asks, ws.Level{u.AskPrice, u.AskSize})
		}
	}
	evt := time.Time{}
	if msg.Timestamp > 0 {
		evt = time.UnixMilli(msg.Timestamp)
	}
	return &ws.Snapshot{Symbol: token, Bids: bids, Asks: asks, EventTime: evt}, nil
}

func (a *Spot) Heartbeat() []byte                { return []byte("PING") }
func (a *Spot) HeartbeatInterval() time.Duration { return 50 * time.Second }
func (a *Spot) PongFor(_ []byte) []byte          { return nil }
func (a *Spot) UseLibPings() bool                { return true }
func (a *Spot) SubscribeDelay() time.Duration    { return 0 }
func (a *Spot) MaxSymbols() int                  { return 200 }
func (a *Spot) DecompressGzip() bool             { return false }

func (a *Spot) OnReconnect() {
	a.mu.Lock()
	a.subs = make(map[string]struct{})
	a.mu.Unlock()
}
