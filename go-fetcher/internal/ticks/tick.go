// Package ticks streams individual trade events (each fill on each venue)
// to clients in real time. This is the "live tick" layer that sits next to
// the orderbook feed.
//
// WHY: depth WebSockets on most venues coalesce updates to ~5-10 push/sec.
// Trade streams push each individual fill — 20-50 events/sec on active
// pairs. UI rendering each trade as a price-level pulse delivers
// arbion-level visible liveness without exceeding venue rate limits.
//
// Architecture:
//
//	venue trade WS → Adapter.Parse → Tick → Hub.OnTick(ex, sym, tick)
//	                                     ↘ Ring (per-symbol, last 50 trades)
//
// One Runner per (venue) — same WS lifecycle pattern as internal/ws but
// the Parse return type is a Tick instead of a Snapshot. No periodic
// state (orderbook diff/snapshot) — each frame is independent.
package ticks

import (
	"context"
	"sync"
	"time"
)

// Side of a trade — derived from the venue's "taker side" or "is buyer
// maker" field. Capitalised "B"/"S" string so JSON serialisation matches
// the existing /ws/book wire format.
type Side string

const (
	Buy  Side = "B" // taker bought from the book
	Sell Side = "S" // taker sold into the book
)

// Tick is a single trade event flattened from the venue's wire format.
//
//	Exchange — adapter cache key ("binance", "mexc", "aster", ...)
//	Symbol   — base token without USDT suffix ("LAB", "BTC", ...)
//	Price    — fill price in quote currency
//	Size     — fill size in base currency
//	Side     — Buy = taker bought; Sell = taker sold
//	TsMS     — venue-provided timestamp, milliseconds since epoch
//	ID       — venue-provided trade id (string for portability)
type Tick struct {
	Exchange string  `json:"e"`
	Symbol   string  `json:"s"`
	Price    float64 `json:"p"`
	Size     float64 `json:"q"`
	Side     Side    `json:"d"`
	TsMS     int64   `json:"t"`
	ID       string  `json:"i,omitempty"`
}

// UpdateFunc is called by the runner on every parsed Tick. Hot path:
// implementations must be cheap (non-blocking channel send + ring push).
type UpdateFunc func(t Tick)

// Adapter mirrors ws.Adapter exactly for everything except Parse.
// We can't reuse ws.Adapter directly because Parse returns *Snapshot,
// not *Tick. The lifecycle (connect, reconnect, heartbeat, gzip,
// watchdog) is identical — see runner.go for the shared loop.
type Adapter interface {
	Name() string
	URL(ctx context.Context) (string, error)
	BuildSubscribe(symbols []string) [][]byte
	// Parse may return multiple ticks per frame (some venues batch trades
	// in a single push). Return (nil, nil) for non-data frames.
	Parse(frame []byte) ([]Tick, error)
	Heartbeat() []byte
	HeartbeatInterval() time.Duration
	PongFor(frame []byte) []byte
	UseLibPings() bool
	SubscribeDelay() time.Duration
	MaxSymbols() int
	DecompressGzip() bool
	OnReconnect()
}

// Ring is a tiny per-symbol circular buffer of recent ticks.
// New-clients-on-/ws/trades get a backfill from this when they
// subscribe, so they don't see an empty UI for the first few seconds.
type Ring struct {
	mu   sync.Mutex
	data map[string][]Tick // key = "<exchange>:<symbol>"
	cap  int
}

// NewRing builds a Ring that keeps `capPerKey` ticks per (exchange, symbol).
// 50 is enough to fill the UI's recent-trades panel on subscribe.
func NewRing(capPerKey int) *Ring {
	if capPerKey <= 0 {
		capPerKey = 50
	}
	return &Ring{
		data: make(map[string][]Tick, 256),
		cap:  capPerKey,
	}
}

// Push records one tick. Oldest entries are evicted when the buffer
// reaches cap. Allocates a new slice on growth — readers see consistent
// snapshots without holding the lock during iteration.
func (r *Ring) Push(t Tick) {
	key := t.Exchange + ":" + t.Symbol
	r.mu.Lock()
	buf := r.data[key]
	if len(buf) >= r.cap {
		// Shift left by one — cheaper than reslicing the head for our
		// small cap (50). One memmove per push at 50 trades is trivial.
		copy(buf, buf[1:])
		buf[len(buf)-1] = t
	} else {
		buf = append(buf, t)
	}
	r.data[key] = buf
	r.mu.Unlock()
}

// Recent returns a copy of the last N ticks for (exchange, symbol),
// most recent last. Empty slice if nothing recorded yet.
func (r *Ring) Recent(exchange, symbol string, n int) []Tick {
	key := exchange + ":" + symbol
	r.mu.Lock()
	buf := r.data[key]
	if n <= 0 || n > len(buf) {
		n = len(buf)
	}
	out := make([]Tick, n)
	copy(out, buf[len(buf)-n:])
	r.mu.Unlock()
	return out
}
