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
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

// Bare /ws — same architecture fix as binance/futures.go (no
// URL+SUBSCRIBE duplicate-subscribe).
const futuresWS = "wss://fstream.asterdex.com/ws"

type Futures struct {
	store *cache.Store
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{store: store}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("aster", snap.Symbol, snap, "ws")
	})
}

func (a *Futures) Name() string                          { return "aster" }
func (a *Futures) URL(_ context.Context) (string, error) { return futuresWS, nil }

func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
	if len(symbols) == 0 {
		return nil
	}
	// Aster (Binance fork) inherits Binance's SUBSCRIBE-frame size limit.
	// Chunk to 200 streams per frame so the same prewarm-1000 set works
	// here as it does for binance.
	const chunkSize = 200
	frames := make([][]byte, 0, (len(symbols)+chunkSize-1)/chunkSize)
	id := time.Now().UnixNano()
	for i := 0; i < len(symbols); i += chunkSize {
		end := i + chunkSize
		if end > len(symbols) {
			end = len(symbols)
		}
		params := make([]string, end-i)
		for j, s := range symbols[i:end] {
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
func (a *Futures) SubscribeDelay() time.Duration    { return 0 }
func (a *Futures) MaxSymbols() int                  { return 200 }
func (a *Futures) DecompressGzip() bool             { return false }
func (a *Futures) OnReconnect()                     {}
