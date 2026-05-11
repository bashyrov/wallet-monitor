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
	"encoding/json"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/symbols"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

const tradesMaxPairsPerClient = 100

// Trades is the /ws/trades channel state.
type Trades struct {
	hub  *Hub
	ring *ticks.Ring
	mgr  *symbols.Manager

	mu   sync.Mutex
	subs map[*client]map[string]struct{} // client → pair set
}

func NewTrades(ring *ticks.Ring, mgr *symbols.Manager) *Trades {
	return &Trades{
		hub:  NewHub("trades"),
		ring: ring,
		mgr:  mgr,
		subs: make(map[*client]map[string]struct{}, 64),
	}
}

func (t *Trades) Hub() *Hub { return t.hub }

// OnTick is called from a tick-stream adapter goroutine for every
// parsed trade. Pushes to all clients subscribed to (exchange, symbol).
func (t *Trades) OnTick(tk ticks.Tick) {
	// Push to ring first so backfill on new subscriber has the freshest data.
	t.ring.Push(tk)

	pairKey := tk.Exchange + ":" + tk.Symbol

	t.mu.Lock()
	if len(t.subs) == 0 {
		t.mu.Unlock()
		return
	}
	var targets []*client
	for c, set := range t.subs {
		if _, ok := set[pairKey]; ok {
			targets = append(targets, c)
		}
	}
	t.mu.Unlock()

	if len(targets) == 0 {
		return
	}

	body, err := json.Marshal(map[string]any{
		"trades": []ticks.Tick{tk},
	})
	if err != nil {
		return
	}
	for _, c := range targets {
		select {
		case c.outbox <- body:
		default:
			// outbox full — Broadcast drops, but we deliberately don't
			// kill the client here. /ws/trades is fire-and-forget; a
			// brief stall is fine. The hub.Broadcast path remains the
			// canonical slow-client reaper.
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
