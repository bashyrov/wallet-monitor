// Package ws is the orderbook-WebSocket framework. The runner.go loop owns
// connection lifecycle, reconnect/backoff, heartbeat dispatch, and the
// stale-frames watchdog. Per-venue subclasses implement only the bits that
// differ — URL, subscribe message, parser.
//
// Bug-resistance design (every Python regression we fixed has a place here):
//
//   bug #1  orjson→TEXT          → SendText() helper, never use raw WriteMessage
//   bug #2  policy reconnect     → Runner has separate policyBackoff timer
//   bug #3  hyperliquid 1011     → handled by same policyBackoff
//   bug #4  bitget app-ping       → Adapter.Heartbeat() returns a text frame
//   bug #5  bingx Ping/Pong gzip → Adapter.PongFor() catches incoming pings
//   bug #6  lib pings ignored    → Adapter.UseLibPings() returns false
//   bug #19 KuCoin sub flood      → Adapter.SubscribeDelay()
//   bug #20 stale TCP             → Runner has watchdog goroutine
package ws

import (
	"context"
	"time"
)

// Side of the book.
type Side int

const (
	Bids Side = iota
	Asks
)

// Level is one (price, size) pair. Floats — orderbook arithmetic is fine in
// double precision at exchange-quote granularity.
type Level [2]float64

// Snapshot is what every Adapter.Parse() returns — already sorted (bids
// best→worst, asks best→worst), capped to ~200 levels per side.
//
// EventTime is the venue-side timestamp of the event when the adapter
// could extract one (zero when unavailable). Used by the metrics layer
// to histogram per-venue end-to-end latency: `time.Since(EventTime)`
// at the broadcast point. Adapters set it from the frame's `T`/`ts`/
// `time` field (units vary per venue — see adapter for conversion).
type Snapshot struct {
	Symbol    string
	Bids      []Level
	Asks      []Level
	EventTime time.Time
}

// UpdateFunc is called by the runner on every parsed snapshot.
//
//	exchange — adapter name ("binance", "bitget_spot", ...)
//	snap     — parsed levels
//
// Implementations must be cheap (book cache write, Redis throttle, file
// flag) — runner is a hot path and must keep up with 500-2000 frames/s/venue.
type UpdateFunc func(exchange string, snap Snapshot)

// Adapter is what each venue implements.
//
// All methods are called from a single goroutine — no synchronisation
// needed inside an adapter. Cross-adapter shared state (cache) is the
// runner's concern.
type Adapter interface {
	// Name returns the cache key prefix ("binance", "bitget_spot", ...).
	// Must be stable for the lifetime of the binary.
	Name() string

	// URL returns the dial URL. May change per-connect (KuCoin's token
	// auth fetches a fresh URL via REST first) — runner re-invokes per
	// connect attempt, never caches.
	URL(ctx context.Context) (string, error)

	// BuildSubscribe returns the JSON frame(s) to send after WS open.
	// Returning multiple frames is allowed (some venues batch subs in
	// chunks of N symbols per frame).
	//
	// IMPORTANT: returned bytes are sent as TEXT frames by the runner
	// (see bug #1). Adapters MUST NOT base64/encode them — return UTF-8
	// JSON bytes only.
	BuildSubscribe(symbols []string) [][]byte

	// Parse converts an inbound frame to a Snapshot. Returns nil snap +
	// nil error to indicate "not a data frame" (subscribe acks, errors,
	// pings handled elsewhere). Returning an error is for parse bugs only —
	// the runner logs and skips.
	Parse(frame []byte) (*Snapshot, error)

	// Heartbeat returns the keep-alive frame to send every interval. nil
	// means "no app-level heartbeat" — the lib's WS-frame ping is enough.
	//
	// Bitget V2 / KuCoin / HTX / BingX all need this — their servers
	// either ignore lib pings or actively close on them (bug #4, #5, #6).
	Heartbeat() []byte

	// HeartbeatInterval — how often to fire Heartbeat(). Must be < the
	// venue's server-side timeout. 25s for Bitget (30s timeout); 15s
	// otherwise.
	HeartbeatInterval() time.Duration

	// PongFor returns the bytes to send back in response to an inbound
	// frame, or nil if the frame is not a ping. BingX sends gzip "Ping"
	// and expects a "Pong" reply — that's exactly what this hook is for
	// (bug #5).
	PongFor(frame []byte) []byte

	// UseLibPings — should the websockets library send WS-frame pings on
	// its own? false for venues that ignore/reject them (Bitget, KuCoin,
	// BingX, HTX). When false, only the app-level Heartbeat() runs.
	UseLibPings() bool

	// SubscribeDelay between successive subscribe frames. KuCoin caps at
	// ~3 subs/sec/conn (bug #19); 400ms gives us ~2.5/s with margin.
	// Most venues return 0 (no delay).
	SubscribeDelay() time.Duration

	// MaxSymbols caps total topics on one connection. BingX caps WS at
	// ~100 symbols; we run REST backstop for the remainder. 0 = unlimited.
	MaxSymbols() int

	// DecompressGzip — for venues that stream gzip-compressed text
	// (HTX, BingX). Runner handles inflation transparently.
	DecompressGzip() bool

	// OnReconnect is called right before each connect attempt so the
	// adapter can clear local snapshot state. Most adapters reset their
	// price→size dict here so a stale book doesn't bleed into the new
	// stream.
	OnReconnect()
}
