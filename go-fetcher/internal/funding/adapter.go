// Package funding is the funding-rate WS framework, parallel to internal/ws
// for orderbooks but with these differences:
//
//   - Always paired with a REST backstop. Python's hardest production
//     bug class was incomplete WS feeds (Bybit volume, OKX price, KuCoin
//     volume+rate, MEXC rate, BingX symbol cap) — REST fills gaps. On Go,
//     a goroutine running every 2s is the natural shape, not a "pure-thread
//     trick" we had to invent for asyncio.
//
//   - Lower frequency (per-symbol funding ticks ~1/sec), so the runner is
//     simpler — no stale-watchdog reconnect storm class.
//
//   - The Adapter returns Tick (not Snapshot) — funding rate, mark price,
//     volume, next-funding timestamp. One Tick per push.
package funding

import (
	"context"
	"time"
)

// Tick is one funding-rate observation.
//
// All fields are optional except Symbol + Rate. REST backstop fills any
// field the WS push omits — caller never sees nulls in the merged feed.
type Tick struct {
	Symbol      string    // canonical token, no suffix ("BTC", not "BTCUSDT")
	Rate        float64   // current funding rate (per 8h, 1h, or 4h depending on venue)
	MarkPrice   float64   // mark price in quote currency
	IndexPrice  float64   // index reference (if exposed)
	Volume24h   float64   // 24h volume in quote currency (USDT/USDC/USD)
	OpenIntUSD  float64   // open interest in USD (some venues only)
	NextFunding time.Time // when the next funding payment lands
	IntervalH   float64   // funding-payment interval in hours (4 or 8 for most)
	UpdatedAt   time.Time // server timestamp if available, else time.Now()
}

// UpdateFunc — runner calls this on every parsed Tick. Cheap (cache write
// + Redis throttle); heavy work belongs on a separate goroutine.
type UpdateFunc func(exchange string, t Tick)

// Adapter — what each venue implements. Two endpoint kinds:
//
//	WS push       — sub-second funding tick (where available)
//	REST backstop — full sweep of all symbols every BackstopInterval
//
// Some venues only expose REST (no WS funding feed) — those return
// "" / nil from the WS methods so the runner skips that path.
type Adapter interface {
	// Name is the venue cache key ("binance", "bybit", ...).
	Name() string

	// ── WS push (optional — return "" to disable WS entirely) ───────────
	URL(ctx context.Context) (string, error)
	BuildSubscribe(symbols []string) [][]byte
	ParseWS(frame []byte) ([]Tick, error)
	Heartbeat() []byte
	HeartbeatInterval() time.Duration
	PongFor(frame []byte) []byte
	UseLibPings() bool
	DecompressGzip() bool

	// ── REST backstop (always required — funding feeds need full sweep) ─
	BackstopFetch(ctx context.Context, symbols []string) ([]Tick, error)
	BackstopInterval() time.Duration
}
