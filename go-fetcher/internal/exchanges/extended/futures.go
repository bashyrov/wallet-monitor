// Package extended — Extended (x10) Starknet-based perp DEX orderbook.
//
// Trade stream lives in trades.go (per-market or aggregated). This file
// adds the orderbook stream per Phase 2o of the plan.
//
// Endpoint: path-based, ONE WS per market or fan-out at base path.
//
//	wss://api.starknet.extended.exchange/stream.extended.exchange/v1/orderbooks/{market}
//
// Wire format (modelled on the trades envelope; unverified — first
// frame in prod logs is the truth check):
//
//	{"ts":<ms>, "seq":N,
//	 "data":{ "m":"BTC-USD",
//	          "type":"SNAPSHOT"|"DELTA",
//	          "b":[["px","sz"], ...],
//	          "a":[["px","sz"], ...] }}
//
// Per the plan: initial SNAPSHOT, then 100ms DELTAs, fresh SNAPSHOT
// every 60s. seq field for ordering — on gap we drop state and let the
// next SNAPSHOT reseed.
//
// CAVEAT: wire format is speculative. If prod returns a different
// shape, prod logs will surface decode errors; adjust the struct then.
// EventTime falls back to top-level `ts` even if the body shape
// changes, so the latency metric still works.
package extended

import (
	"context"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const orderbookBase = "wss://api.starknet.extended.exchange/stream.extended.exchange/v1/orderbooks"

type Futures struct {
	store *cache.Store

	mu    sync.Mutex
	books map[string]*book
	lastSeq map[string]int64
}

type book struct {
	bids map[float64]float64
	asks map[float64]float64
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{
		store:   store,
		books:   make(map[string]*book),
		lastSeq: make(map[string]int64),
	}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("extended", snap.Symbol, snap, "ws")
	})
}

func (a *Futures) Name() string                          { return "extended" }
func (a *Futures) URL(_ context.Context) (string, error) { return orderbookBase, nil }

// BuildSubscribe — Extended uses path-based fan-out at the base URL.
// No SUBSCRIBE frame needed; the URL itself enrolls the connection in
// the all-markets stream.
func (a *Futures) BuildSubscribe(_ []string) [][]byte { return nil }

func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
	// Wire format verified live 2026-05-13: data.b/data.a are arrays of
	// {q, p} objects (quantity, price as strings), NOT [px, sz] arrays.
	// Top-level `type` mirrors `data.t` ("SNAPSHOT" | "DELTA"). `ts` is
	// top-level ms-since-epoch. Delta `q` may be NEGATIVE (size delta) —
	// we treat any non-zero q as the new size of the price level; q=0
	// deletes. Use of relative deltas vs absolute is unverified — first
	// pass uses absolute-replace semantics.
	type level struct {
		Q string `json:"q"`
		P string `json:"p"`
	}
	var msg struct {
		Ts   int64  `json:"ts"`
		Seq  int64  `json:"seq"`
		Type string `json:"type"` // "SNAPSHOT" | "DELTA" mirrored at top level
		Data struct {
			Type   string  `json:"t"`
			Market string  `json:"m"`
			Bids   []level `json:"b"`
			Asks   []level `json:"a"`
		} `json:"data"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	frameType := msg.Data.Type
	if frameType == "" {
		frameType = msg.Type
	}
	if frameType != "SNAPSHOT" && frameType != "DELTA" {
		return nil, nil
	}
	if !strings.HasSuffix(msg.Data.Market, "-USD") {
		return nil, nil
	}
	token := strings.TrimSuffix(msg.Data.Market, "-USD")

	a.mu.Lock()
	defer a.mu.Unlock()

	// Gap detection: monotone seq per market. SNAPSHOT resets.
	prev := a.lastSeq[token]
	if frameType != "SNAPSHOT" && prev != 0 && msg.Seq != prev+1 {
		delete(a.books, token)
		delete(a.lastSeq, token)
		return nil, nil
	}
	a.lastSeq[token] = msg.Seq

	bk, ok := a.books[token]
	if !ok {
		bk = &book{bids: make(map[float64]float64), asks: make(map[float64]float64)}
		a.books[token] = bk
	}
	if frameType == "SNAPSHOT" {
		bk.bids = make(map[float64]float64, len(msg.Data.Bids))
		bk.asks = make(map[float64]float64, len(msg.Data.Asks))
	}
	apply := func(side map[float64]float64, rows []level) {
		for _, r := range rows {
			px, perr := strconv.ParseFloat(r.P, 64)
			if perr != nil {
				continue
			}
			sz, serr := strconv.ParseFloat(r.Q, 64)
			if serr != nil {
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

	var evt time.Time
	if msg.Ts > 0 {
		evt = time.UnixMilli(msg.Ts)
	}
	return &ws.Snapshot{
		Symbol:    token,
		Bids:      ws.SortedLevels(bk.bids, ws.Bids, 200),
		Asks:      ws.SortedLevels(bk.asks, ws.Asks, 200),
		EventTime: evt,
	}, nil
}

func (a *Futures) Heartbeat() []byte                { return nil }
func (a *Futures) HeartbeatInterval() time.Duration { return 0 }
func (a *Futures) PongFor(_ []byte) []byte          { return nil }
func (a *Futures) UseLibPings() bool                { return true }
func (a *Futures) SubscribeDelay() time.Duration    { return 0 }
func (a *Futures) MaxSymbols() int                  { return 0 }
func (a *Futures) DecompressGzip() bool             { return false }

func (a *Futures) OnReconnect() {
	a.mu.Lock()
	a.books = make(map[string]*book)
	a.lastSeq = make(map[string]int64)
	a.mu.Unlock()
}
