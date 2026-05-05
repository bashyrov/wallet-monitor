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
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/redisbus"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/symbols"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const (
	// 25ms — same cadence Python's _book_broadcast_loop runs at.
	bookBroadcastInterval = 25 * time.Millisecond
	// Per-client cap. /arb pair-page needs 2 (long + short side); the
	// screener live-In/Out feature needs ~80 for the top-40 rows. 100 is
	// what Python uses (BOOK_MAX_PAIRS_PER_CLIENT).
	bookMaxPairsPerClient = 100
	// Levels sent per side in the event-driven push. UI shows 8; 20 gives
	// visible depth without ballooning the JSON payload (200 → 20 = 10×
	// less serialization work and network bytes per frame).
	bookBroadcastLevels = 20
)

// Book is the /ws/book channel state.
type Book struct {
	hub    *Hub
	reader *redisbus.Reader
	store  *cache.Store    // fallback when Redis is unavailable
	mgr    *symbols.Manager // for Touch on subscribe — keeps the WS alive

	mu   sync.Mutex
	subs map[*client]map[string]float64 // client → pair → last-ts-sent
}

func NewBook(reader *redisbus.Reader, store *cache.Store, mgr *symbols.Manager) *Book {
	return &Book{
		hub:    NewHub("book"),
		reader: reader,
		store:  store,
		mgr:    mgr,
		subs:   make(map[*client]map[string]float64, 64),
	}
}

func (b *Book) Hub() *Hub { return b.hub }

// OnBookUpdate is called from cache.Store's onUpdate hook on every OB snapshot.
// It bypasses the 25ms Redis-poll cycle and pushes directly to clients
// subscribed to this pair — eliminating the polling lag entirely.
// The tick() safety-net still runs; it skips pairs whose TS has already
// been sent by this path (lastTS updated here before releasing the lock).
func (b *Book) OnBookUpdate(exchange, symbol string, bids, asks []ws.Level) {
	pairKey := exchange + ":" + symbol

	b.mu.Lock()
	if len(b.subs) == 0 {
		b.mu.Unlock()
		return
	}
	now := float64(time.Now().UnixMilli()) / 1000.0
	var targets []*client
	for c, subs := range b.subs {
		if _, ok := subs[pairKey]; ok {
			subs[pairKey] = now // mark sent so tick() skips the duplicate
			targets = append(targets, c)
		}
	}
	b.mu.Unlock()

	if len(targets) == 0 {
		return
	}

	nb := bids
	if len(nb) > bookBroadcastLevels {
		nb = nb[:bookBroadcastLevels]
	}
	na := asks
	if len(na) > bookBroadcastLevels {
		na = na[:bookBroadcastLevels]
	}
	bidSlice := make([][]float64, len(nb))
	for i, lv := range nb {
		bidSlice[i] = []float64{lv[0], lv[1]}
	}
	askSlice := make([][]float64, len(na))
	for i, lv := range na {
		askSlice[i] = []float64{lv[0], lv[1]}
	}
	body, err := json.Marshal(map[string]any{
		"books": map[string]any{
			pairKey: map[string]any{
				"ts":   now,
				"bids": bidSlice,
				"asks": askSlice,
			},
		},
	})
	if err != nil {
		return
	}
	for _, c := range targets {
		select {
		case c.outbox <- body:
		default:
		}
	}
	log.L().Trace().Str("pair", pairKey).Int("clients", len(targets)).Msg("book realtime push")
}

// Run drives the periodic broadcast tick. One MGET per tick across the
// union of all subscribed pairs (fixed cost regardless of client count).
func (b *Book) Run(ctx context.Context) {
	t := time.NewTicker(bookBroadcastInterval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
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
	b.mu.Lock()
	delete(b.subs, c)
	b.mu.Unlock()
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

	if len(added) > 0 {
		log.L().Debug().Int("uid", c.uid).Strs("pairs", added).Msg("book subscribe")
	}
}

func (b *Book) handleUnsubscribe(c *client, pairs []string) {
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
		delete(subs, np)
	}
	b.mu.Unlock()
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
