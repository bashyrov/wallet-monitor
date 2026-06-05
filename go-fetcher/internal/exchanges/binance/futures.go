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
	"os"
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
	useBBO bool // BINANCE_USE_BBO=1 → @bookTicker channel; false → @depth20@100ms

	// Phase 2a — adapter is now stateful so the @bookTicker BBO frames
	// (event-driven, scalar b/B/a/A) can splice over the 20-level
	// @depth20@100ms state between depth pushes. Each depth frame
	// REPLACES books[sym] wholesale (depth20 is a full-snapshot stream,
	// not delta); BBO only ever touches bbo[sym].
	stateMu sync.Mutex
	books   map[string]*book
	bbo     map[string]*bboLevel
}

type book struct {
	bids map[float64]float64
	asks map[float64]float64
}

type bboLevel struct {
	bidPx, bidSz float64
	askPx, askSz float64
}

// NewFutures returns a Runner ready to call .Run(ctx) on.
// Set env BINANCE_USE_BBO=1 to switch the channel from @depth20@100ms
// to @bookTicker (real-time BBO, event-driven, ~30-100 updates/sec).
func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{
		store:  store,
		filter: NewFuturesTradingFilter(),
		books:  make(map[string]*book),
		bbo:    make(map[string]*bboLevel),
		useBBO: os.Getenv("BINANCE_USE_BBO") == "1",
	}
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
	//
	// BINANCE_USE_BBO=1: hybrid dual-track, same as Bybit/OKX/Bitget.
	// URL includes BOTH @depth20@100ms AND @bookTicker per symbol.
	// MaxSymbols=100 in BBO mode → 100 × 2 = 200 streams — safely below
	// the ~400-stream threshold that caused 1008 in the 2026-05-13 hotfix
	// (which used 200 sym × 2 = 400 streams).
	if len(syms) == 0 {
		base := futuresCombinedBase + "?streams=btcusdt@depth20@100ms"
		if a.useBBO {
			base += "/btcusdt@bookTicker"
		}
		return base, nil
	}
	var parts []string
	for _, s := range syms {
		lower := strings.ToLower(s) + "usdt"
		parts = append(parts, lower+"@depth20@100ms")
		if a.useBBO {
			parts = append(parts, lower+"@bookTicker")
		}
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
	// Dual-track when BINANCE_USE_BBO=1: one SUBSCRIBE frame per channel
	// (depth first, then bookTicker), 100 symbols per frame = 100 streams
	// per frame. This mirrors how Bitget/OKX chunk their dual subs and
	// avoids exceeding Binance's undocumented per-frame stream limit.
	channels := []string{"usdt@depth20@100ms"}
	if a.useBBO {
		channels = append(channels, "usdt@bookTicker")
	}
	const chunkSize = 100
	id := time.Now().UnixNano()
	frames := make([][]byte, 0, len(channels)*((len(listed)+chunkSize-1)/chunkSize))
	for ci, ch := range channels {
		for i := 0; i < len(listed); i += chunkSize {
			end := i + chunkSize
			if end > len(listed) {
				end = len(listed)
			}
			chunk := make([]string, end-i)
			for j, s := range listed[i:end] {
				chunk[j] = strings.ToLower(s) + ch
			}
			frame := map[string]any{
				"method": "SUBSCRIBE",
				"params": chunk,
				"id":     id + int64(ci*1000+i),
			}
			b, _ := ws.MarshalJSON(frame)
			frames = append(frames, b)
		}
	}
	return frames
}

// Parse one frame.
//
// Two stream types now routed (Phase 2a):
//
//	@depth20@100ms — combined-stream wrapper with `b`/`a` as arrays:
//	  {"stream":"btcusdt@depth20@100ms",
//	   "data":{"e":"depthUpdate","s":"BTCUSDT","b":[["px","sz"],...],"a":[...]}}
//
//	@bookTicker — scalar `b`/`B`/`a`/`A` (top bid/ask px+qty):
//	  {"stream":"btcusdt@bookTicker",
//	   "data":{"e":"bookTicker","u":N,"s":"BTCUSDT","b":"px","B":"qty","a":"px","A":"qty"}}
//
// Both feed SEPARATE state stores (books vs bbo); mergedSnapshot
// splices BBO over depth top at emit time. depth20 is full-replace
// (every 100ms it's the current top-20 — not a delta).
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
		return nil, nil
	}

	// Route by stream suffix.
	isBookTicker := strings.HasSuffix(wrap.Stream, "@bookTicker")
	isDepth := strings.Contains(wrap.Stream, "@depth")

	// Extract symbol from the stream prefix.
	var sym string
	if wrap.Stream != "" {
		s := wrap.Stream
		if i := strings.IndexByte(s, '@'); i > 0 {
			sym = strings.ToUpper(s[:i])
		}
	}

	dataBytes := []byte(wrap.Data)
	if len(dataBytes) == 0 {
		// Bare stream form — try parsing the whole frame.
		dataBytes = frame
	}

	if isBookTicker {
		return a.parseBookTicker(sym, dataBytes)
	}
	if isDepth {
		return a.parseDepth(sym, dataBytes)
	}
	// Fall back to old detection when stream prefix is absent (rare).
	return a.parseDepth(sym, dataBytes)
}

func (a *Futures) parseDepth(sym string, dataBytes []byte) (*ws.Snapshot, error) {
	var inner struct {
		Event   string     `json:"e"` // decoy: absorbs the string event-type field so case-insensitive sonic doesn't route it to EvTime
		Symbol  string     `json:"s"`
		EvTime  int64      `json:"E"` // event time ms (depth + bookTicker)
		TradeTs int64      `json:"T"` // transaction time ms (depth has both)
		B       [][]string `json:"b"`
		A       [][]string `json:"a"`
		Bids    [][]string `json:"bids"`
		Asks    [][]string `json:"asks"`
	}
	if err := ws.UnmarshalJSON(dataBytes, &inner); err != nil {
		return nil, err
	}
	if sym == "" {
		sym = strings.ToUpper(inner.Symbol)
	}
	if !strings.HasSuffix(sym, "USDT") {
		return nil, nil
	}
	token := strings.TrimSuffix(sym, "USDT")
	if !a.filter.IsTrading(context.Background(), sym) {
		return nil, nil
	}

	bids := inner.B
	asks := inner.A
	if len(bids) == 0 && len(asks) == 0 {
		bids, asks = inner.Bids, inner.Asks
	}

	// Full-replace from depth20 — every 100ms it's the top-20 snapshot.
	a.stateMu.Lock()
	bk, ok := a.books[token]
	if !ok {
		bk = &book{bids: map[float64]float64{}, asks: map[float64]float64{}}
		a.books[token] = bk
	}
	bk.bids = make(map[float64]float64, len(bids))
	bk.asks = make(map[float64]float64, len(asks))
	for _, lv := range parseLevels(bids) {
		bk.bids[lv[0]] = lv[1]
	}
	for _, lv := range parseLevels(asks) {
		bk.asks[lv[0]] = lv[1]
	}
	snap := a.mergedSnapshotLocked(token)
	a.stateMu.Unlock()
	switch {
	case inner.TradeTs > 0:
		snap.EventTime = time.UnixMilli(inner.TradeTs)
	case inner.EvTime > 0:
		snap.EventTime = time.UnixMilli(inner.EvTime)
	}
	return snap, nil
}

func (a *Futures) parseBookTicker(sym string, dataBytes []byte) (*ws.Snapshot, error) {
	// Scalar shape: {e:"bookTicker",u,s,b,B,a,A,T,E}
	var inner struct {
		Event   string `json:"e"` // decoy: absorb string event-type before case-insensitive routing to EvTime
		Symbol  string `json:"s"`
		B       string `json:"b"` // best bid price
		Bq      string `json:"B"` // best bid qty
		A       string `json:"a"` // best ask price
		Aq      string `json:"A"` // best ask qty
		EvTime  int64  `json:"E"` // event time ms
		TradeTs int64  `json:"T"` // transaction time ms (matching engine)
	}
	if err := ws.UnmarshalJSON(dataBytes, &inner); err != nil {
		return nil, err
	}
	if sym == "" {
		sym = strings.ToUpper(inner.Symbol)
	}
	if !strings.HasSuffix(sym, "USDT") {
		return nil, nil
	}
	token := strings.TrimSuffix(sym, "USDT")
	if !a.filter.IsTrading(context.Background(), sym) {
		return nil, nil
	}

	bidPx, _ := strconv.ParseFloat(inner.B, 64)
	bidSz, _ := strconv.ParseFloat(inner.Bq, 64)
	askPx, _ := strconv.ParseFloat(inner.A, 64)
	askSz, _ := strconv.ParseFloat(inner.Aq, 64)

	a.stateMu.Lock()
	b, ok := a.bbo[token]
	if !ok {
		b = &bboLevel{}
		a.bbo[token] = b
	}
	// Binance @bookTicker always reports the current top — never sends
	// zero-size as a "remove" signal (the top always exists on a live
	// pair). Cache verbatim.
	b.bidPx, b.bidSz = bidPx, bidSz
	b.askPx, b.askSz = askPx, askSz
	snap := a.mergedSnapshotLocked(token)
	a.stateMu.Unlock()
	switch {
	case inner.TradeTs > 0:
		snap.EventTime = time.UnixMilli(inner.TradeTs)
	case inner.EvTime > 0:
		snap.EventTime = time.UnixMilli(inner.EvTime)
	}
	return snap, nil
}

// mergedSnapshotLocked — must hold stateMu. Same splice semantics as
// Bybit / OKX / Bitget: BBO at strictly better px prepends; same px
// refreshes size; worse no-ops.
func (a *Futures) mergedSnapshotLocked(token string) *ws.Snapshot {
	bk := a.books[token]
	var bids, asks []ws.Level
	if bk != nil {
		bids = ws.SortedLevels(bk.bids, ws.Bids, 200)
		asks = ws.SortedLevels(bk.asks, ws.Asks, 200)
	}
	if b := a.bbo[token]; b != nil {
		bids = spliceBBOBid(bids, b.bidPx, b.bidSz)
		asks = spliceBBOAsk(asks, b.askPx, b.askSz)
	}
	return &ws.Snapshot{Symbol: token, Bids: bids, Asks: asks}
}

func spliceBBOBid(bids []ws.Level, bboPx, bboSz float64) []ws.Level {
	if bboPx <= 0 {
		return bids
	}
	if len(bids) == 0 {
		return []ws.Level{{bboPx, bboSz}}
	}
	if bboPx > bids[0][0] {
		return append([]ws.Level{{bboPx, bboSz}}, bids...)
	}
	if bboPx == bids[0][0] {
		bids[0][1] = bboSz
	}
	return bids
}

func spliceBBOAsk(asks []ws.Level, bboPx, bboSz float64) []ws.Level {
	if bboPx <= 0 {
		return asks
	}
	if len(asks) == 0 {
		return []ws.Level{{bboPx, bboSz}}
	}
	if bboPx < asks[0][0] {
		return append([]ws.Level{{bboPx, bboSz}}, asks...)
	}
	if bboPx == asks[0][0] {
		asks[0][1] = bboSz
	}
	return asks
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
// as one. In BBO mode we send 2 SUBSCRIBE frames (depth + bookTicker)
// for each 100-symbol chunk; 250ms gap = 4 msg/s, well under the 5/s
// ceiling. In depth-only mode we send 1 frame per 100-symbol chunk.
func (a *Futures) SubscribeDelay() time.Duration { return 250 * time.Millisecond }
func (a *Futures) MaxSymbols() int {
	if a.useBBO {
		// 100 sym × 2 streams (depth20 + bookTicker) = 200 streams in URL.
		// Safely below the ~400-stream threshold that caused 1008 when we
		// last tried dual-track at 200 syms (2026-05-13 hotfix).
		return 100
	}
	return 200
}
func (a *Futures) DecompressGzip() bool { return false }
func (a *Futures) OnReconnect() {
	a.stateMu.Lock()
	a.books = make(map[string]*book)
	a.bbo = make(map[string]*bboLevel)
	a.stateMu.Unlock()
}
