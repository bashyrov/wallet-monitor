// Package aster — Aster DEX is a Binance fork. Same protocol on a different
// host, same partial-book stream format. We embed *binance.Futures
// behaviourally by copying the parser and swapping the URL — embedding
// directly would force one shared exchangeInfo cache between the two
// venues, which we don't want (different symbol sets).
//
// Bug-resistance: same as binance — TEXT frames, watchdog, policy backoff,
// trading-filter (Aster also returns SETTLING/BREAK status on delisted
// pairs that linger in /fapi/v1/exchangeInfo).
package aster

import (
	"context"
	"encoding/json"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

// Combined-stream base — see binance/futures.go for why we moved off
// the bare /ws + SUBSCRIBE flow at large prewarm sizes.
const futuresCombinedBase = "wss://fstream.asterdex.com/stream"

type Futures struct {
	store  *cache.Store
	filter *tradingFilter
	mu     sync.Mutex
	syms   []string
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{store: store, filter: newTradingFilter()}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("aster", snap.Symbol, snap, "ws")
	})
}

func (a *Futures) Name() string { return "aster" }

func (a *Futures) URL(_ context.Context) (string, error) {
	a.mu.Lock()
	syms := a.syms
	a.mu.Unlock()
	if len(syms) == 0 {
		return futuresCombinedBase + "?streams=btcusdt@depth20@100ms", nil
	}
	parts := make([]string, len(syms))
	for i, s := range syms {
		parts[i] = strings.ToLower(s) + "usdt@depth20@100ms"
	}
	return futuresCombinedBase + "?streams=" + strings.Join(parts, "/"), nil
}

func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
	// Filter against Aster exchangeInfo — Aster (Binance fork) closes
	// the connection with 1008 if even one stream in a SUBSCRIBE frame
	// names a non-listed symbol. Cross-venue prewarm-1000 inevitably
	// contains symbols Aster doesn't list. Filter once at subscribe
	// time; IsTrading returns true on REST error so we fail-open.
	ctx := context.Background()
	listed := make([]string, 0, len(symbols))
	for _, s := range symbols {
		if a.filter.IsTrading(ctx, strings.ToUpper(s)+"USDT") {
			listed = append(listed, s)
		}
	}
	a.mu.Lock()
	a.syms = append(a.syms[:0], listed...)
	a.mu.Unlock()
	if len(listed) == 0 {
		return nil
	}
	const chunkSize = 200
	frames := make([][]byte, 0, (len(listed)+chunkSize-1)/chunkSize)
	id := time.Now().UnixNano()
	for i := 0; i < len(listed); i += chunkSize {
		end := i + chunkSize
		if end > len(listed) {
			end = len(listed)
		}
		params := make([]string, end-i)
		for j, s := range listed[i:end] {
			params[j] = strings.ToLower(s) + "usdt@depth20@100ms"
		}
		frame := map[string]any{"method": "SUBSCRIBE", "params": params, "id": id + int64(i)}
		b, _ := ws.MarshalJSON(frame)
		frames = append(frames, b)
	}
	return frames
}

func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
	var wrap struct {
		Stream string          `json:"stream"`
		Data   json.RawMessage `json:"data"`
		Result *any            `json:"result"`
	}
	if err := ws.UnmarshalJSON(frame, &wrap); err != nil {
		return nil, err
	}
	if wrap.Result != nil {
		return nil, nil
	}

	var sym string
	if wrap.Stream != "" {
		if i := strings.IndexByte(wrap.Stream, '@'); i > 0 {
			sym = strings.ToUpper(wrap.Stream[:i])
		}
	}

	var inner struct {
		Symbol string     `json:"s"`
		B      [][]string `json:"b"`
		A      [][]string `json:"a"`
		Bids   [][]string `json:"bids"`
		Asks   [][]string `json:"asks"`
	}
	// Bare /ws frames have e/E/T/s/b/a at top level (no `data` wrapper) —
	// fall back to parsing the whole frame when wrap.Data is empty.
	dataBytes := []byte(wrap.Data)
	if len(dataBytes) == 0 {
		dataBytes = frame
	}
	_ = ws.UnmarshalJSON(dataBytes, &inner)
	if sym == "" {
		sym = strings.ToUpper(inner.Symbol)
	}

	bids, asks := inner.B, inner.A
	if len(bids) == 0 && len(asks) == 0 {
		bids, asks = inner.Bids, inner.Asks
	}
	if !strings.HasSuffix(sym, "USDT") {
		return nil, nil
	}
	token := strings.TrimSuffix(sym, "USDT")

	snap := &ws.Snapshot{Symbol: token}
	for _, r := range bids {
		if len(r) < 2 {
			continue
		}
		px, _ := strconv.ParseFloat(r[0], 64)
		sz, _ := strconv.ParseFloat(r[1], 64)
		if sz > 0 {
			snap.Bids = append(snap.Bids, ws.Level{px, sz})
		}
	}
	for _, r := range asks {
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

func (a *Futures) Heartbeat() []byte                { return nil }
func (a *Futures) HeartbeatInterval() time.Duration { return 0 }
func (a *Futures) PongFor(_ []byte) []byte          { return nil }
func (a *Futures) UseLibPings() bool                { return true }

// Aster (Binance fork) inherits the public-WS 5 msg/s rate limit. With
// chunked SUBSCRIBE frames, a 250ms delay keeps us under that ceiling.
func (a *Futures) SubscribeDelay() time.Duration { return 250 * time.Millisecond }
func (a *Futures) MaxSymbols() int               { return 200 }
func (a *Futures) DecompressGzip() bool             { return false }
func (a *Futures) OnReconnect()                     {}
