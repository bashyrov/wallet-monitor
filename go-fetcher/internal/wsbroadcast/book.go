// book.go — /ws/book channel.
//
// Per-client subscription model. Unlike long-short and funding (which
// fan-out a single payload), each client subscribes to a subset of
// `<exchange>:<symbol>` pairs and only receives updates for those pairs.
//
// Wire protocol (matches Python's /ws/book):
//
//   Client → first frame {"auth":"<JWT>"}                    (required)
//          → {"action":"subscribe",   "pairs":["binance:BTC", ...]}
//          → {"action":"unsubscribe", "pairs":[...]}
//          → text "ping" → server replies "pong"
//
//   Server → {"books": {"<ex>:<SYM>": {"ts":..,"bids":[...],"asks":[...]}, ...}}
//
// Read path: one MGET to Redis per broadcast tick across the union of
// all subscribed pairs. Per-client diff: only push pairs whose ts has
// advanced since the last frame this client received. This matches the
// Python broadcaster's behaviour byte-for-byte; the only divergence is
// the fallback file-memo path is dropped here — Go has the cache store
// in-process and uses Redis as the canonical read.
package wsbroadcast

import (
	"context"
	"encoding/json"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/obsmetrics"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/redisbus"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/symbols"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

// bookBroadcastIntervalDefault — 1s, safety-net cadence.
// Real-time updates flow through OnBookUpdate (push-through) — this
// tick only catches pairs where the Redis MGET path needs to fill in
// for a missed Store callback. Was 25ms; bumped because push-through
// makes the tick redundant on the hot path.
const bookBroadcastIntervalDefault = 1 * time.Second

// bookBroadcastInterval is set from env var AVALANT_BOOK_TICK_INTERVAL
// at init time (e.g. "500ms", "2s", "100ms"). Defaults to 1s.
var bookBroadcastInterval = func() time.Duration {
	if v := os.Getenv("AVALANT_BOOK_TICK_INTERVAL"); v != "" {
		if d, err := time.ParseDuration(v); err == nil && d > 0 {
			return d
		}
	}
	return bookBroadcastIntervalDefault
}()

// bookFlushInterval — how often the per-pair pending buffer is flushed
// to subscribers. This is the hard ceiling on update frequency visible
// to the client (updates/sec = 1000 / bookFlushInterval_ms).
// Controlled by env AVALANT_BOOK_FLUSH_INTERVAL (e.g. "50ms", "33ms").
// Default: 50ms = 20 updates/sec. Was hardcoded 200ms = 5 updates/sec.
var bookFlushInterval = func() time.Duration {
	if v := os.Getenv("AVALANT_BOOK_FLUSH_INTERVAL"); v != "" {
		if d, err := time.ParseDuration(v); err == nil && d >= 10*time.Millisecond {
			return d
		}
	}
	return 50 * time.Millisecond
}()

const (
	// Per-client cap. /arb pair-page needs 2 (long + short side); the
	// screener live-In/Out feature needs ~80 for the top-40 rows. 100 is
	// what Python uses (BOOK_MAX_PAIRS_PER_CLIENT).
	bookMaxPairsPerClient = 100
	// Levels sent per side in the event-driven push. UI shows 8; 20 gives
	// visible depth without ballooning the JSON payload (200 → 20 = 10×
	// less serialization work and network bytes per frame).
	bookBroadcastLevels = 20

	// hotPairFloor — per-pair minimum gap between two event-driven pushes
	// even when the symbol is "hot" (event-driven bypass). Protects the
	// frontend from 200+/sec event storms on active BTC during volatile
	// moments. 10ms = 100 Hz ceiling, well below any rendering bottleneck
	// but plenty of headroom over the 50ms pending-flush ceiling.
	hotPairFloor = 10 * time.Millisecond
)

// tieredFreshness enables the three-class freshness model:
//
//	CLASS 1 (cold, screener table + watchlist): 2s aggregate (longshort.go)
//	CLASS 2 (hot/opened pair): event-driven bypass-pending in book.go
//	CLASS 3 (hot/alert): same as Class 2, marked via Book.MarkAlertHot
//
// Disabled by default — keep existing flush-pending behaviour byte-compatible.
var tieredFreshness = strings.TrimSpace(os.Getenv("AVALANT_TIERED_FRESHNESS")) == "1"

// Book is the /ws/book channel state.
type Book struct {
	hub    *Hub
	reader *redisbus.Reader
	store  *cache.Store     // fallback when Redis is unavailable
	mgr    *symbols.Manager // for Touch on subscribe — keeps the WS alive

	mu   sync.Mutex
	subs map[*client]map[string]float64 // client → pair → last-ts-sent

	// Per-pair pending update buffer. Hot venues fire OnBookUpdate at
	// 50-100/sec; pushing each as its own WS frame floods the browser
	// (same class of problem as /ws/trades pre-aggregation). Book is
	// stateful, so the buffer stores only the LATEST snapshot per pair
	// — older snapshots within a flush window are invisible anyway.
	pendMu  sync.Mutex
	pending map[string]bookPending

	// ── Tiered freshness (Class 2/3) ─────────────────────────────────────
	// hotMu guards both maps. Refcount of "currently subscribed by N clients"
	// for symbols a user has opened on /arb (Class 2). alertHot stores
	// symbols flagged by an active spread alert (Class 3). isHotKey() checks
	// the union.
	hotMu          sync.RWMutex
	hotSubsRefcnt  map[string]int       // symbol → # client subscriptions (Class 2)
	alertHot       map[string]struct{}  // symbol → present iff active alert (Class 3)
	lastHotPushAt  map[string]time.Time // pairKey ("ex:SYM") → last bypass-push (floor enforcement)
}

type bookPending struct {
	ts   float64
	bids []ws.Level
	asks []ws.Level
}

func NewBook(reader *redisbus.Reader, store *cache.Store, mgr *symbols.Manager) *Book {
	return &Book{
		hub:           NewHub("book"),
		reader:        reader,
		store:         store,
		mgr:           mgr,
		subs:          make(map[*client]map[string]float64, 64),
		pending:       make(map[string]bookPending, 256),
		hotSubsRefcnt: make(map[string]int, 64),
		alertHot:      make(map[string]struct{}, 32),
		lastHotPushAt: make(map[string]time.Time, 128),
	}
}

// isHot returns true if the symbol (extracted from pairKey "ex:SYM") is in
// the Class 2 ∪ Class 3 hot set. Cheap: RLock + two map lookups.
func (b *Book) isHot(symbol string) bool {
	if !tieredFreshness {
		return false
	}
	b.hotMu.RLock()
	defer b.hotMu.RUnlock()
	if _, ok := b.alertHot[symbol]; ok {
		return true
	}
	return b.hotSubsRefcnt[symbol] > 0
}

// MarkAlertHot sets/clears the Class 3 hot flag for a symbol. Called by
// the alert-sync loop reading active_alerts.json. No-op if tiered off.
func (b *Book) MarkAlertHot(symbol string, hot bool) {
	if !tieredFreshness {
		return
	}
	b.hotMu.Lock()
	if hot {
		b.alertHot[symbol] = struct{}{}
	} else {
		delete(b.alertHot, symbol)
	}
	b.hotMu.Unlock()
}

// ReplaceAlertHot atomically swaps the entire Class 3 set. Used by the
// dump reader to apply the latest active-alert state in one shot.
func (b *Book) ReplaceAlertHot(symbols []string) {
	if !tieredFreshness {
		return
	}
	next := make(map[string]struct{}, len(symbols))
	for _, s := range symbols {
		if s != "" {
			next[strings.ToUpper(strings.TrimSpace(s))] = struct{}{}
		}
	}
	b.hotMu.Lock()
	b.alertHot = next
	total := len(b.hotSubsRefcnt) + len(b.alertHot)
	b.hotMu.Unlock()
	obsmetrics.SetHotSetSize(total)
}

// reportHotSize refreshes the hot-set gauge. Called from sub/unsub paths
// where the refcount map changes. Cheap: single Lock + atomic store.
func (b *Book) reportHotSize() {
	if !tieredFreshness {
		return
	}
	b.hotMu.RLock()
	n := len(b.hotSubsRefcnt) + len(b.alertHot)
	b.hotMu.RUnlock()
	obsmetrics.SetHotSetSize(n)
}

func (b *Book) Hub() *Hub { return b.hub }

// OnBookUpdate is called from cache.Store's onUpdate hook on every OB
// snapshot. Instead of pushing directly (50-100/sec on hot venues —
// floods browser clients), the snapshot lands in a per-pair buffer
// that overwrites any previous pending snapshot. The book Run-loop
// flushes the buffer once per bookBroadcastInterval (1s default) and
// fans out one frame per pair.
//
// Net wire rate: O(1) frame per pair per flush, regardless of how
// fast the underlying venue pushes.
func (b *Book) OnBookUpdate(exchange, symbol string, bids, asks []ws.Level) {
	pairKey := exchange + ":" + symbol
	// Trim levels HERE before storing — keeps the buffer small and the
	// marshal step at flush time trivial.
	nb := bids
	if len(nb) > bookBroadcastLevels {
		nb = nb[:bookBroadcastLevels]
	}
	na := asks
	if len(na) > bookBroadcastLevels {
		na = na[:bookBroadcastLevels]
	}
	nowMs := time.Now()
	ts := float64(nowMs.UnixMilli()) / 1000.0

	// Tiered freshness: if this symbol is in the hot set (Class 2 or 3),
	// bypass the pending buffer and push event-driven straight to subscribed
	// clients, respecting the hotPairFloor (10ms) per-pair gap. This is
	// what turns BBO updates from a 50ms-ceiling stream into a 100Hz-cap
	// event-driven stream on the open pair.
	if tieredFreshness && b.isHot(symbol) {
		b.hotMu.Lock()
		last, ok := b.lastHotPushAt[pairKey]
		if ok && nowMs.Sub(last) < hotPairFloor {
			// Below floor: fall through to pending buffer so the flush
			// path still picks it up — never silently drop a snapshot.
			b.hotMu.Unlock()
			obsmetrics.BookHotFloorDrops.Inc(pairKey)
			b.pendMu.Lock()
			b.pending[pairKey] = bookPending{ts: ts, bids: nb, asks: na}
			b.pendMu.Unlock()
			return
		}
		b.lastHotPushAt[pairKey] = nowMs
		b.hotMu.Unlock()
		obsmetrics.BookBypassPushes.Inc(pairKey)
		b.pushBypass(pairKey, ts, nb, na)
		return
	}

	b.pendMu.Lock()
	b.pending[pairKey] = bookPending{ts: ts, bids: nb, asks: na}
	b.pendMu.Unlock()
}

// pushBypass sends one /ws/book frame for pairKey to every subscribed
// client immediately, bypassing the per-pair pending buffer. Called from
// OnBookUpdate when the symbol is in the hot set.
func (b *Book) pushBypass(pairKey string, ts float64, bids, asks []ws.Level) {
	bidSlice := make([][]float64, len(bids))
	for i, lv := range bids {
		bidSlice[i] = []float64{lv[0], lv[1]}
	}
	askSlice := make([][]float64, len(asks))
	for i, lv := range asks {
		askSlice[i] = []float64{lv[0], lv[1]}
	}
	body, err := json.Marshal(map[string]any{
		"books": map[string]any{
			pairKey: map[string]any{
				"ts":   ts,
				"bids": bidSlice,
				"asks": askSlice,
			},
		},
	})
	if err != nil {
		return
	}
	b.mu.Lock()
	for c, set := range b.subs {
		if _, ok := set[pairKey]; !ok {
			continue
		}
		set[pairKey] = ts
		select {
		case c.outbox <- body:
		default:
		}
	}
	b.mu.Unlock()
}

// flushPending drains the per-pair buffer and pushes one frame per
// pair to its subscribers. Called from Run on every tick.
func (b *Book) flushPending() {
	b.pendMu.Lock()
	if len(b.pending) == 0 {
		b.pendMu.Unlock()
		return
	}
	pending := b.pending
	b.pending = make(map[string]bookPending, len(pending))
	b.pendMu.Unlock()

	b.mu.Lock()
	if len(b.subs) == 0 {
		b.mu.Unlock()
		return
	}
	subsSnap := make(map[*client]map[string]float64, len(b.subs))
	for c, set := range b.subs {
		subsSnap[c] = set
	}
	b.mu.Unlock()

	for pairKey, snap := range pending {
		// Skip pairs no client wants.
		anyWants := false
		for _, set := range subsSnap {
			if _, ok := set[pairKey]; ok {
				anyWants = true
				break
			}
		}
		if !anyWants {
			continue
		}
		bidSlice := make([][]float64, len(snap.bids))
		for i, lv := range snap.bids {
			bidSlice[i] = []float64{lv[0], lv[1]}
		}
		askSlice := make([][]float64, len(snap.asks))
		for i, lv := range snap.asks {
			askSlice[i] = []float64{lv[0], lv[1]}
		}
		body, err := json.Marshal(map[string]any{
			"books": map[string]any{
				pairKey: map[string]any{
					"ts":   snap.ts,
					"bids": bidSlice,
					"asks": askSlice,
				},
			},
		})
		if err != nil {
			continue
		}
		// Update last-ts-sent under the subs mutex so tick() doesn't
		// re-emit the same snapshot from its MGET fallback.
		b.mu.Lock()
		for c, set := range b.subs {
			if _, ok := set[pairKey]; !ok {
				continue
			}
			set[pairKey] = snap.ts
			select {
			case c.outbox <- body:
			default:
			}
		}
		b.mu.Unlock()
	}
}

// Run drives two periodic ticks:
//   - flushPending every bookFlushInterval (default 50ms = 20/sec) —
//     drains the per-pair OB buffer populated by OnBookUpdate.
//     Controlled by env AVALANT_BOOK_FLUSH_INTERVAL. Was hardcoded
//     200ms (5/sec) — the binding ceiling for client update frequency.
//   - tick every bookBroadcastInterval (1s default) — MGET fallback
//     for pairs whose direct push didn't fire (Redis blip).
func (b *Book) Run(ctx context.Context) {
	flushT := time.NewTicker(bookFlushInterval)
	defer flushT.Stop()
	t := time.NewTicker(bookBroadcastInterval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-flushT.C:
			b.flushPending()
		case <-t.C:
			b.flushPending()
			b.tick(ctx)
		}
	}
}

func (b *Book) tick(ctx context.Context) {
	// Collect every unique pair across all subscribed clients. One MGET
	// per tick — identical CPU cost regardless of client count.
	b.mu.Lock()
	if len(b.subs) == 0 {
		b.mu.Unlock()
		return
	}
	allPairs := make([]string, 0, 64)
	seen := make(map[string]struct{}, 64)
	for _, subs := range b.subs {
		for p := range subs {
			if _, ok := seen[p]; ok {
				continue
			}
			seen[p] = struct{}{}
			allPairs = append(allPairs, p)
		}
	}
	clientsSnap := make([]*client, 0, len(b.subs))
	for c := range b.subs {
		clientsSnap = append(clientsSnap, c)
	}
	b.mu.Unlock()

	entries := b.reader.ReadBooks(ctx, allPairs)

	// Build per-client payloads. Per-pair last-ts tracking is per-client
	// so each socket only ever sees frames it hasn't seen.
	for _, c := range clientsSnap {
		b.mu.Lock()
		subs, ok := b.subs[c]
		if !ok {
			b.mu.Unlock()
			continue
		}
		payload := map[string]map[string]any{}
		for pair, lastTS := range subs {
			entry, found := entries[pair]
			if !found {
				// Fallback: try the in-process cache store. Tolerates
				// brief Redis blips without dropping the feed.
				if e, ok := b.fallbackFromStore(pair); ok {
					entry = e
					found = true
				}
			}
			if !found {
				continue
			}
			if entry.TS <= lastTS {
				continue
			}
			data := entry.Data
			payload[pair] = map[string]any{
				"ts":   entry.TS,
				"bids": data["bids"],
				"asks": data["asks"],
			}
			subs[pair] = entry.TS
		}
		b.mu.Unlock()
		if len(payload) == 0 {
			continue
		}
		body, err := json.Marshal(map[string]any{"books": payload})
		if err != nil {
			continue
		}
		select {
		case c.outbox <- body:
		default:
			// Slow client — its writer goroutine will deregister it
			// on the next outbox-full hit. Just skip this tick.
		}
	}
}

// fallbackFromStore reads from the in-process orderbook cache when
// Redis is empty/missing. Same shape as redisbus.BookEntry.
func (b *Book) fallbackFromStore(pair string) (redisbus.BookEntry, bool) {
	if b.store == nil {
		return redisbus.BookEntry{}, false
	}
	ex, sym, ok := splitPair(pair)
	if !ok {
		return redisbus.BookEntry{}, false
	}
	e, ok := b.store.Get(ex, sym)
	if !ok {
		return redisbus.BookEntry{}, false
	}
	bids := make([][]float64, 0, len(e.Bids))
	for _, lv := range e.Bids {
		bids = append(bids, []float64{lv[0], lv[1]})
	}
	asks := make([][]float64, 0, len(e.Asks))
	for _, lv := range e.Asks {
		asks = append(asks, []float64{lv[0], lv[1]})
	}
	return redisbus.BookEntry{
		TS:   float64(time.Now().UnixMilli()) / 1000.0,
		Data: map[string][][]float64{"bids": bids, "asks": asks},
	}, true
}

// register adds a client to the book hub and creates an empty subs map.
func (b *Book) register(c *client) {
	b.hub.register(c)
	b.mu.Lock()
	b.subs[c] = make(map[string]float64, 4)
	b.mu.Unlock()
}

// deregister cleans up the client's subs map. The hub's deregister
// handles the socket close + log; this just nulls the side-table entry.
func (b *Book) deregister(c *client) {
	var droppedSyms []string
	b.mu.Lock()
	if subs, ok := b.subs[c]; ok && tieredFreshness {
		droppedSyms = make([]string, 0, len(subs))
		for p := range subs {
			if _, sym, ok := splitPair(p); ok {
				droppedSyms = append(droppedSyms, sym)
			}
		}
	}
	delete(b.subs, c)
	b.mu.Unlock()

	if tieredFreshness && len(droppedSyms) > 0 {
		b.hotMu.Lock()
		for _, sym := range droppedSyms {
			if b.hotSubsRefcnt[sym] > 0 {
				b.hotSubsRefcnt[sym]--
				if b.hotSubsRefcnt[sym] == 0 {
					delete(b.hotSubsRefcnt, sym)
				}
			}
		}
		b.hotMu.Unlock()
		b.reportHotSize()
	}
}

// runReader replaces hub.runReader for /ws/book — same keep-alive +
// app-level ping handling but adds subscribe/unsubscribe parsing.
func (b *Book) runReader(c *client) {
	defer func() {
		b.deregister(c)
		b.hub.deregister(c)
	}()
	c.conn.SetReadLimit(64 * 1024)
	_ = c.conn.SetReadDeadline(time.Now().Add(pongTimeout))
	c.conn.SetPongHandler(func(string) error {
		_ = c.conn.SetReadDeadline(time.Now().Add(pongTimeout))
		return nil
	})
	for {
		mt, data, err := c.conn.ReadMessage()
		if err != nil {
			return
		}
		if mt != 1 { // websocket.TextMessage = 1
			continue
		}
		if len(data) == 4 && string(data) == "ping" {
			select {
			case c.outbox <- []byte("pong"):
			default:
			}
			continue
		}
		var msg struct {
			Action string   `json:"action"`
			Pairs  []string `json:"pairs"`
		}
		if err := json.Unmarshal(data, &msg); err != nil {
			continue
		}
		switch strings.ToLower(msg.Action) {
		case "subscribe":
			b.handleSubscribe(c, msg.Pairs)
		case "unsubscribe":
			b.handleUnsubscribe(c, msg.Pairs)
		}
	}
}

func (b *Book) handleSubscribe(c *client, pairs []string) {
	cleaned := make([]string, 0, len(pairs))
	for _, p := range pairs {
		if np, ok := normalizePair(p); ok {
			cleaned = append(cleaned, np)
		}
	}
	b.mu.Lock()
	subs, ok := b.subs[c]
	if !ok {
		b.mu.Unlock()
		return
	}
	free := bookMaxPairsPerClient - len(subs)
	if free < 0 {
		free = 0
	}
	added := make([]string, 0, len(cleaned))
	for i, p := range cleaned {
		if i >= free {
			break
		}
		if _, exists := subs[p]; !exists {
			subs[p] = 0
			added = append(added, p)
		}
	}
	b.mu.Unlock()

	// Touch the symbol manager so the orderbook adapter keeps the WS
	// subscribed to these pairs. Without this, pairs outside the prewarm
	// set fall out within ~120 s and the user's stream dries up.
	if b.mgr != nil {
		for _, p := range added {
			ex, sym, ok := splitPair(p)
			if !ok {
				continue
			}
			b.mgr.Touch(ex, sym)
		}
	}

	// Tiered freshness: each newly-added subscription bumps the per-symbol
	// hot refcount. Symbol becomes Class-2 hot iff any client is subscribed.
	if tieredFreshness && len(added) > 0 {
		b.hotMu.Lock()
		for _, p := range added {
			_, sym, ok := splitPair(p)
			if !ok {
				continue
			}
			b.hotSubsRefcnt[sym]++
		}
		b.hotMu.Unlock()
		b.reportHotSize()
	}

	if len(added) > 0 {
		log.L().Debug().Int("uid", c.uid).Strs("pairs", added).Msg("book subscribe")
	}
}

func (b *Book) handleUnsubscribe(c *client, pairs []string) {
	removed := make([]string, 0, len(pairs))
	b.mu.Lock()
	subs, ok := b.subs[c]
	if !ok {
		b.mu.Unlock()
		return
	}
	for _, p := range pairs {
		np, ok := normalizePair(p)
		if !ok {
			continue
		}
		if _, was := subs[np]; was {
			delete(subs, np)
			removed = append(removed, np)
		}
	}
	b.mu.Unlock()

	if tieredFreshness && len(removed) > 0 {
		b.hotMu.Lock()
		for _, p := range removed {
			_, sym, ok := splitPair(p)
			if !ok {
				continue
			}
			if b.hotSubsRefcnt[sym] > 0 {
				b.hotSubsRefcnt[sym]--
				if b.hotSubsRefcnt[sym] == 0 {
					delete(b.hotSubsRefcnt, sym)
				}
			}
		}
		b.hotMu.Unlock()
		b.reportHotSize()
	}
}

// normalizePair mirrors Python's _normalize_pair: lower-case exchange,
// upper-case symbol, alnum + underscore only, max 24 chars per side.
func normalizePair(raw string) (string, bool) {
	idx := strings.Index(raw, ":")
	if idx <= 0 {
		return "", false
	}
	ex := strings.TrimSpace(strings.ToLower(raw[:idx]))
	sym := strings.TrimSpace(strings.ToUpper(raw[idx+1:]))
	if ex == "" || sym == "" || len(ex) > 24 || len(sym) > 24 {
		return "", false
	}
	if !isPairToken(ex) || !isPairToken(sym) {
		return "", false
	}
	return ex + ":" + sym, true
}

func isPairToken(s string) bool {
	for _, r := range s {
		ok := (r >= '0' && r <= '9') || (r >= 'a' && r <= 'z') ||
			(r >= 'A' && r <= 'Z') || r == '_'
		if !ok {
			return false
		}
	}
	return true
}

func splitPair(pair string) (string, string, bool) {
	idx := strings.Index(pair, ":")
	if idx <= 0 || idx >= len(pair)-1 {
		return "", "", false
	}
	return pair[:idx], pair[idx+1:], true
}
