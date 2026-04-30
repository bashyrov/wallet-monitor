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

const futuresWS = "wss://fstream.asterdex.com/stream?streams="

type Futures struct {
	store *cache.Store
	syms  []string
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{store: store}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("aster", snap.Symbol, snap, "ws")
	})
}

func (a *Futures) Name() string { return "aster" }

func (a *Futures) URL(_ context.Context) (string, error) {
	if len(a.syms) == 0 {
		return futuresWS + "btcusdt@depth20@100ms", nil
	}
	parts := make([]string, len(a.syms))
	for i, s := range a.syms {
		parts[i] = strings.ToLower(s) + "usdt@depth20@100ms"
	}
	return futuresWS + strings.Join(parts, "/"), nil
}

func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
	a.syms = symbols
	if len(symbols) == 0 {
		return nil
	}
	params := make([]string, len(symbols))
	for i, s := range symbols {
		params[i] = strings.ToLower(s) + "usdt@depth20@100ms"
	}
	frame := map[string]any{"method": "SUBSCRIBE", "params": params, "id": time.Now().UnixNano()}
	b, _ := ws.MarshalJSON(frame)
	return [][]byte{b}
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
	if len(wrap.Data) > 0 {
		_ = ws.UnmarshalJSON(wrap.Data, &inner)
	}
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
