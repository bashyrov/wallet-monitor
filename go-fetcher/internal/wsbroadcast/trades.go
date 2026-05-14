// trades.go — /ws/trades channel.
//
// Per-client subscription model identical to /ws/book: each socket
// subscribes to a subset of `<exchange>:<symbol>` pairs and receives
// only matching trade events. New clients get a small backfill (last
// 50 trades per subscribed pair) so the UI panel doesn't show empty
// on first frame.
//
// Wire protocol:
//
//   Client → first frame {"auth":"<JWT>"}
//          → {"action":"subscribe",   "pairs":["binance:BTC", ...]}
//          → {"action":"unsubscribe", "pairs":[...]}
//          → text "ping" → server replies "pong"
//
//   Server → {"trades":[{"e":"binance","s":"BTC","p":..,"q":..,"d":"B","t":..,"i":".."}]}
//
// Push-driven: OnTick fires from the tick-stream adapter goroutine and
// fans out to every subscriber of the matching pair. No polling tick.
package wsbroadcast

import (
	"context"
	"encoding/json"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/symbols"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

const (
	tradesMaxPairsPerClient = 100

	// Server-side trade aggregation: instead of pushing a WS frame per
	// raw tick (hot Binance pairs run 100-300/sec, which overwhelmed
	// browser clients enough that Chrome flagged tabs as memory hogs),
	// buffer trades per pair and flush at this cadence. 100 ms matches
	// the human-perception threshold for "tick liveness" — anything
	// finer is invisible to the eye anyway.
	tradesFlushInterval = 100 * time.Millisecond
	// Hard cap on the per-pair flush batch — beyond this we drop the
	// oldest. Real hot pairs run ~30 trades/100ms, so 200 is a generous
	// safety net; the cap is there so a burst can't blow the WS message.
	tradesFlushBatchMax = 200
)

// Trades is the /ws/trades channel state.
type Trades struct {
	hub  *Hub
	ring *ticks.Ring
	mgr  *symbols.Manager

	mu   sync.Mutex
	subs map[*client]map[string]struct{} // client → pair set

	// Per-pair pending buffer drained by the flush loop. Keeps trade
	// frames at ~10/sec on the wire regardless of how many raw ticks
	// the adapter pushed. See tradesFlushInterval.
	pendMu  sync.Mutex
	pending map[string][]ticks.Tick // pairKey → queued ticks
}

func NewTrades(ring *ticks.Ring, mgr *symbols.Manager) *Trades {
	return &Trades{
		hub:     NewHub("trades"),
		ring:    ring,
		mgr:     mgr,
		subs:    make(map[*client]map[string]struct{}, 64),
		pending: make(map[string][]ticks.Tick, 256),
	}
}

func (t *Trades) Hub() *Hub { return t.hub }

// Run starts the periodic flush. cmd/fetcher/main.go must call this in
// a goroutine for trade aggregation to actually fire.
func (t *Trades) Run(ctx context.Context) {
	ticker := time.NewTicker(tradesFlushInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			t.flush()
		}
	}
}

// OnTick is called from a tick-stream adapter goroutine for every
// parsed trade. The tick lands in the per-pair pending buffer; the
// flush loop fans out to subscribers at tradesFlushInterval cadence.
func (t *Trades) OnTick(tk ticks.Tick) {
	// Push to ring first so backfill on new subscriber has the freshest data.
	t.ring.Push(tk)

	pairKey := tk.Exchange + ":" + tk.Symbol

	t.pendMu.Lock()
	buf := t.pending[pairKey]
	if len(buf) >= tradesFlushBatchMax {
		// Burst overflow — drop oldest. Single contiguous trim instead
		// of growing unboundedly.
		buf = buf[len(buf)-tradesFlushBatchMax+1:]
	}
	t.pending[pairKey] = append(buf, tk)
	t.pendMu.Unlock()
}

// flush drains the pending buffer once. Marshals one frame per pair
// holding ALL ticks accumulated since the previous flush, fans out to
// every client subscribed to that pair.
func (t *Trades) flush() {
	t.pendMu.Lock()
	if len(t.pending) == 0 {
		t.pendMu.Unlock()
		return
	}
	pending := t.pending
	t.pending = make(map[string][]ticks.Tick, len(pending))
	t.pendMu.Unlock()

	t.mu.Lock()
	hasSubs := len(t.subs) > 0
	// Snapshot subs into a local for the rest of the work — keeps the
	// fan-out off the mutex so OnTick doesn't queue behind it.
	subsSnap := make(map[*client]map[string]struct{}, len(t.subs))
	if hasSubs {
		for c, set := range t.subs {
			subsSnap[c] = set
		}
	}
	t.mu.Unlock()

	if !hasSubs {
		return
	}

	for pairKey, batch := range pending {
		if len(batch) == 0 {
			continue
		}
		var body []byte
		for c, set := range subsSnap {
			if _, ok := set[pairKey]; !ok {
				continue
			}
			if body == nil {
				b, err := json.Marshal(map[string]any{"trades": batch})
				if err != nil {
					break
				}
				body = b
			}
			select {
			case c.outbox <- body:
			default:
				// Slow client — drop this flush for them. /ws/trades is
				// fire-and-forget per the original design note.
			}
		}
	}
}

func (t *Trades) register(c *client) {
	t.hub.register(c)
	t.mu.Lock()
	t.subs[c] = make(map[string]struct{}, 4)
	t.mu.Unlock()
}

func (t *Trades) deregister(c *client) {
	t.mu.Lock()
	delete(t.subs, c)
	t.mu.Unlock()
}

// runReader replaces hub.runReader for /ws/trades — adds subscribe /
// unsubscribe parsing. Mirrors Book.runReader exactly.
func (t *Trades) runReader(c *client) {
	defer func() {
		t.deregister(c)
		t.hub.deregister(c)
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
			t.handleSubscribe(c, msg.Pairs)
		case "unsubscribe":
			t.handleUnsubscribe(c, msg.Pairs)
		}
	}
}

func (t *Trades) handleSubscribe(c *client, pairs []string) {
	cleaned := make([]string, 0, len(pairs))
	for _, p := range pairs {
		if np, ok := normalizePair(p); ok {
			cleaned = append(cleaned, np)
		}
	}
	t.mu.Lock()
	set, ok := t.subs[c]
	if !ok {
		t.mu.Unlock()
		return
	}
	free := tradesMaxPairsPerClient - len(set)
	if free < 0 {
		free = 0
	}
	added := make([]string, 0, len(cleaned))
	for i, p := range cleaned {
		if i >= free {
			break
		}
		if _, exists := set[p]; !exists {
			set[p] = struct{}{}
			added = append(added, p)
		}
	}
	t.mu.Unlock()

	// Touch symbol manager so the venue's tick adapter keeps the WS
	// subscribed even for pairs outside the prewarm set.
	if t.mgr != nil {
		for _, p := range added {
			ex, sym, ok := splitPair(p)
			if !ok {
				continue
			}
			t.mgr.Touch(ex, sym)
		}
	}

	// Backfill — send the most recent ring contents for each newly
	// added pair so the UI panel renders immediately.
	if len(added) > 0 && t.ring != nil {
		backfill := []ticks.Tick{}
		for _, p := range added {
			ex, sym, ok := splitPair(p)
			if !ok {
				continue
			}
			backfill = append(backfill, t.ring.Recent(ex, sym, 50)...)
		}
		if len(backfill) > 0 {
			body, err := json.Marshal(map[string]any{"trades": backfill})
			if err == nil {
				select {
				case c.outbox <- body:
				default:
				}
			}
		}
		log.L().Debug().Int("uid", c.uid).Strs("pairs", added).Msg("trades subscribe")
	}
}

func (t *Trades) handleUnsubscribe(c *client, pairs []string) {
	t.mu.Lock()
	set, ok := t.subs[c]
	if !ok {
		t.mu.Unlock()
		return
	}
	for _, p := range pairs {
		np, ok := normalizePair(p)
		if !ok {
			continue
		}
		delete(set, np)
	}
	t.mu.Unlock()
}
