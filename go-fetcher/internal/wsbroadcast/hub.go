// hub.go — connection registry + fan-out for one WS channel.
//
// Each channel (long-short, funding, book) owns one Hub. Connections
// register on accept and deregister on close. The channel's broadcast
// loop calls hub.Broadcast([]byte) which fans out to every client via
// its outbox goroutine — slow clients don't block fast ones, and a
// dropped/disconnected client is reaped without touching the loop.
package wsbroadcast

import (
	"sync"
	"time"

	"github.com/gorilla/websocket"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
)

// outbox-buffer per client. If a client's outbox fills up (slow consumer),
// we drop the connection rather than blocking the broadcast loop. 128
// messages × ~10 KB diff payload ≈ 1.3 MB max queued before drop —
// generous head-room for a brief network blip without unbounded memory.
const clientOutboxSize = 128

// pongTimeout — how long we wait for a client's WS-level pong reply
// before considering the connection dead. App-level "ping"/"pong" text
// frames are also handled (the existing Python clients send those), but
// the gorilla pong handler is the load-bearing keepalive.
const pongTimeout = 60 * time.Second

type client struct {
	id     uint64
	uid    int
	conn   *websocket.Conn
	outbox chan []byte
	done   chan struct{}
}

// Hub is the per-channel client registry. Safe for concurrent
// register/deregister/broadcast.
type Hub struct {
	name string

	mu      sync.RWMutex
	clients map[*client]struct{}
	nextID  uint64
}

func NewHub(name string) *Hub {
	return &Hub{
		name:    name,
		clients: make(map[*client]struct{}, 64),
	}
}

func (h *Hub) Count() int {
	h.mu.RLock()
	defer h.mu.RUnlock()
	return len(h.clients)
}

// register adds a client. Returns the client for downstream cleanup.
func (h *Hub) register(c *client) {
	h.mu.Lock()
	c.id = h.nextID
	h.nextID++
	h.clients[c] = struct{}{}
	h.mu.Unlock()
	log.L().Debug().Str("ch", h.name).Uint64("id", c.id).Int("uid", c.uid).
		Int("total", len(h.clients)).Msg("ws connect")
}

func (h *Hub) deregister(c *client) {
	h.mu.Lock()
	if _, ok := h.clients[c]; !ok {
		h.mu.Unlock()
		return
	}
	delete(h.clients, c)
	h.mu.Unlock()
	close(c.done)
	// c.outbox is intentionally NOT closed here. Broadcast() and OnBookUpdate()
	// hold stale client snapshots and would panic on send-to-closed-channel.
	// runWriter exits cleanly via <-c.done instead.
	_ = c.conn.Close()
	log.L().Debug().Str("ch", h.name).Uint64("id", c.id).Int("uid", c.uid).
		Int("total", len(h.clients)).Msg("ws disconnect")
}

// Broadcast queues the message to every connected client. Slow clients
// whose outbox fills up are dropped on the next attempt — keeps the
// broadcast goroutine wait-free in the steady state.
func (h *Hub) Broadcast(msg []byte) {
	h.mu.RLock()
	snap := make([]*client, 0, len(h.clients))
	for c := range h.clients {
		snap = append(snap, c)
	}
	h.mu.RUnlock()
	for _, c := range snap {
		select {
		case c.outbox <- msg:
		default:
			// Slow client. Drop the connection — its outbox is full.
			log.L().Warn().Str("ch", h.name).Uint64("id", c.id).Msg("ws slow client, dropping")
			go h.deregister(c)
		}
	}
}

// runWriter — per-client goroutine that drains outbox and writes to
// the WS connection. One writer per client so a single slow socket
// can't block the broadcast loop.
func (h *Hub) runWriter(c *client) {
	defer h.deregister(c)
	pingTicker := time.NewTicker(30 * time.Second)
	defer pingTicker.Stop()
	for {
		select {
		case <-c.done:
			return
		case msg, ok := <-c.outbox:
			if !ok {
				return
			}
			_ = c.conn.SetWriteDeadline(time.Now().Add(10 * time.Second))
			if err := c.conn.WriteMessage(websocket.TextMessage, msg); err != nil {
				return
			}
		case <-pingTicker.C:
			_ = c.conn.SetWriteDeadline(time.Now().Add(5 * time.Second))
			if err := c.conn.WriteMessage(websocket.PingMessage, nil); err != nil {
				return
			}
		}
	}
}

// runReader — per-client goroutine that reads incoming frames.
// We only care about app-level "ping" text (some clients send this) and
// the websocket-level pong (handled via gorilla's pong handler set up
// at accept). Anything else is silently consumed.
func (h *Hub) runReader(c *client) {
	defer h.deregister(c)
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
		// App-level ping/pong text frame — match Python's behaviour.
		if mt == websocket.TextMessage && len(data) == 4 && string(data) == "ping" {
			select {
			case c.outbox <- []byte("pong"):
			default:
			}
		}
	}
}
