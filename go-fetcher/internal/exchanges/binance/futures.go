// Package binance implements the Binance USDT-margined perp orderbook WS.
//
// Channel: <symbol>@depth20@100ms — full snapshot every 100ms (max 20 levels).
// We use the snapshot variant (not @depth, the diff variant) because:
//
//   - Diff requires a separate REST snapshot fetch + sequence ID tracking.
//     That's more code for the same effective freshness.
//   - 20 levels is enough for the screener UI; the trade panel tops out at
//     show 8 levels per side.
//
// Subscribe shape (combined-stream URL form):
//
//	wss://fstream.binance.com/stream?streams=btcusdt@depth20@100ms/ethusdt@depth20@100ms/...
//
// Inbound shape:
//
//	{"stream": "btcusdt@depth20@100ms",
//	 "data":   {"lastUpdateId": ..., "bids": [["64500.10","0.123"], ...], "asks": [...]}}
//
// Bug-resistance:
//   - Bug #1  (TEXT frame)        : SendText() in runner — adapter doesn't even call WriteMessage
//   - Bug #2  (policy storm 1008) : runner's policyBackoff handles
//   - Bug #8  (delisted NTRN)     : tradingFilter.IsTrading() checked in Parse()
//   - Bug #20 (stale TCP)         : runner's watchdog
//   - Bug #22 (canonical limits)  : not relevant — depth20 is fixed
package binance

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

// Combined-stream base. With AVALANT_PREWARM_TOP_N=1000 the SUBSCRIBE-
// method approach kept hitting Binance's 1008 policy violation (it has
// some undocumented threshold — frame chunking + 250ms delay still got
// rejected at 600 streams). Combined-stream URL bypasses SUBSCRIBE
// entirely: the URL itself enumerates the streams and Binance just
// starts pushing data. Same pattern binance_spot uses successfully.
const futuresCombinedBase = "wss://fstream.binance.com/stream"

// Futures is the ws.Adapter implementation for Binance USDT-perp.
type Futures struct {
	store  *cache.Store
	filter *tradingFilter
	mu     sync.Mutex
	syms   []string
}

// NewFutures returns a Runner ready to call .Run(ctx) on.
func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{store: store, filter: NewFuturesTradingFilter()}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("binance", snap.Symbol, snap, "ws")
	})
}

func (a *Futures) Name() string { return "binance" }

func (a *Futures) URL(_ context.Context) (string, error) {
	a.mu.Lock()
	syms := a.syms
	a.mu.Unlock()
	// First dial happens before BuildSubscribe — fall back to BTC so the
	// dial succeeds. Symbol manager then re-URLs on the next reconcile.
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
	// Filter against exchangeInfo so the combined-stream URL doesn't
	// include non-listed symbols (Binance ignores those silently in
	// URL form, no error — but no point sending them either).
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
	// Combined-stream URL already carries the subscriptions. Also emit
	// a SUBSCRIBE frame so symbol additions on existing connections
	// (after reconnect-suppress) still take effect — Binance accepts
	// chunked SUBSCRIBE on /stream endpoints too.
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
		frame := map[string]any{
			"method": "SUBSCRIBE",
			"params": params,
			"id":     id + int64(i),
		}
		b, _ := ws.MarshalJSON(frame)
		frames = append(frames, b)
	}
	return frames
}

// Parse one frame.
//
// Combined-stream wrapper for the diff-book stream that @depth20@100ms
// actually serves (despite docs implying snapshot):
//
//	{"stream": "btcusdt@depth20@100ms",
//	 "data":   {"e":"depthUpdate","E":...,"T":...,"s":"BTCUSDT","U":...,"u":...,
//	            "pu":...,"b":[["px","sz"], ...],"a":[...]}}
//
// Note: live probing showed @depth20 returns full snapshots-as-diffs (every
// 100ms, capped at 20 levels per side). We don't try to validate the U/u
// continuity — just trust each frame's b/a as the current top-of-book.
//
// Two-pass parse: sonic gets confused when outer/inner structs have
// colliding json tags ("s" present at multiple levels), so we decode the
// wrapper first, then re-decode the inner `data` only if needed.
func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
	var wrap struct {
		Stream string          `json:"stream"`
		Data   json.RawMessage `json:"data"`
		Result *any            `json:"result"` // SUBSCRIBE ack
	}
	if err := ws.UnmarshalJSON(frame, &wrap); err != nil {
		return nil, err
	}

	if wrap.Result != nil {
		// Subscribe-ack: {"result":null,"id":...}
		return nil, nil
	}

	// Pull symbol out of the stream prefix (e.g. "btcusdt@depth20@100ms" →
	// "BTCUSDT"). Live data confirmed the wrapper always has stream set;
	// fallback to bare-stream parsing only if absent.
	dataBytes := []byte(wrap.Data)
	var sym string
	switch {
	case wrap.Stream != "":
		s := wrap.Stream
		if i := strings.IndexByte(s, '@'); i > 0 {
			sym = strings.ToUpper(s[:i])
		}
	default:
		// bare stream (rare) — try the frame itself for s
		dataBytes = frame
	}

	var inner struct {
		Symbol  string     `json:"s"`
		EvTime  int64      `json:"E"` // event time ms (depth + bookTicker)
		TradeTs int64      `json:"T"` // transaction time ms (depth has both)
		B       [][]string `json:"b"`
		A       [][]string `json:"a"`
		Bids    [][]string `json:"bids"`
		Asks    [][]string `json:"asks"`
	}
	if len(dataBytes) > 0 {
		if err := ws.UnmarshalJSON(dataBytes, &inner); err != nil {
			return nil, err
		}
	}
	if sym == "" {
		sym = strings.ToUpper(inner.Symbol)
	}

	bids := inner.B
	asks := inner.A
	if len(bids) == 0 && len(asks) == 0 {
		bids, asks = inner.Bids, inner.Asks
	}

	if !strings.HasSuffix(sym, "USDT") {
		return nil, nil
	}
	token := strings.TrimSuffix(sym, "USDT")

	// Bug #8 — drop delisted symbols. cheap (in-memory map lookup).
	if !a.filter.IsTrading(context.Background(), sym) {
		return nil, nil
	}

	snap := &ws.Snapshot{Symbol: token}
	snap.Bids = parseLevels(bids)
	snap.Asks = parseLevels(asks)
	// Prefer transaction time (T) when present (more accurate matching-engine
	// time); fall back to event time (E). bookTicker has only E.
	switch {
	case inner.TradeTs > 0:
		snap.EventTime = time.UnixMilli(inner.TradeTs)
	case inner.EvTime > 0:
		snap.EventTime = time.UnixMilli(inner.EvTime)
	}
	return snap, nil
}

func parseLevels(rows [][]string) []ws.Level {
	out := make([]ws.Level, 0, len(rows))
	for _, r := range rows {
		if len(r) < 2 {
			continue
		}
		px, perr := strconv.ParseFloat(r[0], 64)
		sz, serr := strconv.ParseFloat(r[1], 64)
		if perr != nil || serr != nil {
			continue
		}
		if sz <= 0 {
			continue
		}
		out = append(out, ws.Level{px, sz})
	}
	return out
}

// Heartbeat — Binance answers WS-level pings; no app-level heartbeat needed.
func (a *Futures) Heartbeat() []byte                { return nil }
func (a *Futures) HeartbeatInterval() time.Duration { return 0 }

// PongFor — Binance doesn't send app-level pings either.
func (a *Futures) PongFor(_ []byte) []byte { return nil }

// UseLibPings — true. gorilla's default behaviour (no auto-pings; we
// would need to enable them explicitly) is fine — Binance answers
// 1011 keepalive on lib-pings within the configured timeout.
func (a *Futures) UseLibPings() bool { return true }

// Binance public WS allows 5 messages/sec. Each SUBSCRIBE chunk counts
// as one. With 1000-stream prewarm split into 5 chunks of 200, sending
// them back-to-back hits 70+ msg/s and the server returns 1008 policy
// violation. 250 ms = 4 msg/s, comfortably under the 5/s ceiling with
// margin for any other client noise on the same connection.
func (a *Futures) SubscribeDelay() time.Duration { return 250 * time.Millisecond }
func (a *Futures) MaxSymbols() int               { return 200 }
func (a *Futures) DecompressGzip() bool          { return false }
func (a *Futures) OnReconnect()                  {}
