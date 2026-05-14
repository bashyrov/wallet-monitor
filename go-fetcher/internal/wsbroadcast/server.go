// server.go — HTTP server exposing the WS endpoints.
//
// Routes:
//   /api/screener/ws/long-short  — canonical Long/Short feed
//   /api/screener/ws/arb         — legacy alias, same data as long-short
//   /api/screener/ws/funding     — full funding rates (deferred — Python still owns)
//   /api/screener/ws/book        — per-pair orderbook diffs (deferred)
//
// Auth: matches Python — first text frame must be {"auth":"<JWT>"} within
// 5 s of accept. For long-short / funding (public feeds) auth failure is
// non-fatal and the client is treated as anonymous (uid=0). For book
// auth is required.
package wsbroadcast

import (
	"context"
	"encoding/json"
	"net/http"
	"time"

	"github.com/gorilla/websocket"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/metrics"
)

const authReadTimeout = 5 * time.Second

// authMode controls the policy applied to the first-frame auth read.
type authMode int

const (
	authOptional authMode = iota // public feeds — no token = anon (uid=0)
	authRequired                 // /ws/book and the like — bad token = close 4401
)

// Service ties one set of channels to one HTTP server. main.go wires
// it with the existing cache.Store + Redis reader (for /ws/book).
type Service struct {
	jwt       *JWTValidator
	longShort *LongShort
	funding   *Funding
	book      *Book
	trades    *Trades
	upgrader  websocket.Upgrader
}

func NewService(jwt *JWTValidator, longShort *LongShort, funding *Funding, book *Book, trades *Trades) *Service {
	return &Service{
		jwt:       jwt,
		longShort: longShort,
		funding:   funding,
		book:      book,
		trades:    trades,
		upgrader: websocket.Upgrader{
			ReadBufferSize:  4 * 1024,
			WriteBufferSize: 16 * 1024,
			CheckOrigin: func(r *http.Request) bool {
				// Behind nginx — Origin is whatever the user's browser
				// sends. Same-site cookie + JWT auth do the gating, not
				// the Origin header.
				return true
			},
		},
	}
}

func (s *Service) Routes(mux *http.ServeMux) {
	mux.HandleFunc("/api/screener/ws/long-short", s.handleLongShort)
	mux.HandleFunc("/api/screener/ws/arb", s.handleLongShort) // legacy alias
	mux.HandleFunc("/api/screener/ws/funding", s.handleFunding)
	mux.HandleFunc("/api/screener/ws/book", s.handleBook)
	if s.trades != nil {
		mux.HandleFunc("/api/screener/ws/trades", s.handleTrades)
	}
	mux.Handle("/metrics", metrics.HTTPHandler())
}

// Run starts the broadcast loops. Should be launched in its own
// goroutine via the errgroup in main.
func (s *Service) Run(ctx context.Context) {
	go s.longShort.Run(ctx)
	go s.funding.Run(ctx)
	go s.book.Run(ctx)
	// trades is push-driven (OnTick) but now uses a per-pair pending
	// buffer drained on a tick — see Trades.flush. Without this Run,
	// OnTick still appends but nothing ever flushes to clients.
	if s.trades != nil {
		go s.trades.Run(ctx)
	}
	<-ctx.Done()
}

// handleTrades upgrades and registers the client on the trades hub.
// Auth OPTIONAL — public trade data (same policy as /ws/long-short).
// Anonymous visitors get uid=0, paid users get their uid. Useful for
// the screener/arb pages to flash trade pulses without forcing login.
func (s *Service) handleTrades(w http.ResponseWriter, r *http.Request) {
	conn, err := s.upgrader.Upgrade(w, r, nil)
	if err != nil {
		log.L().Debug().Err(err).Msg("ws upgrade failed")
		return
	}
	uid, ok := s.handshakeAuth(conn, authOptional)
	if !ok {
		return
	}
	c := &client{
		uid:    uid,
		conn:   conn,
		outbox: make(chan []byte, clientOutboxSize),
		done:   make(chan struct{}),
	}
	s.trades.register(c)
	go s.trades.hub.runWriter(c)
	go s.trades.runReader(c)
}

// handleLongShort upgrades and registers the client on the long-short
// hub. Public feed → optional auth.
func (s *Service) handleLongShort(w http.ResponseWriter, r *http.Request) {
	conn, err := s.upgrader.Upgrade(w, r, nil)
	if err != nil {
		log.L().Debug().Err(err).Msg("ws upgrade failed")
		return
	}
	uid, ok := s.handshakeAuth(conn, authOptional)
	if !ok {
		return
	}
	c := &client{
		uid:    uid,
		conn:   conn,
		outbox: make(chan []byte, clientOutboxSize),
		done:   make(chan struct{}),
	}
	s.longShort.hub.register(c)
	// Send the initial snapshot so the client can render before the
	// next diff lands.
	if snap := s.longShort.SnapshotForNewClient(); snap != nil {
		select {
		case c.outbox <- snap:
		default:
		}
	}
	go s.longShort.hub.runWriter(c)
	go s.longShort.hub.runReader(c)
}

// handleBook upgrades and registers the client on the book hub.
// Auth is REQUIRED — orderbook depth is a paid feed and we want a uid
// on every connection for fair-use tracking. Reader runs the book's
// own loop (vs hub.runReader) because /ws/book accepts in-band
// subscribe/unsubscribe commands the other channels don't.
func (s *Service) handleBook(w http.ResponseWriter, r *http.Request) {
	conn, err := s.upgrader.Upgrade(w, r, nil)
	if err != nil {
		log.L().Debug().Err(err).Msg("ws upgrade failed")
		return
	}
	uid, ok := s.handshakeAuth(conn, authRequired)
	if !ok {
		return
	}
	c := &client{
		uid:    uid,
		conn:   conn,
		outbox: make(chan []byte, clientOutboxSize),
		done:   make(chan struct{}),
	}
	s.book.register(c)
	go s.book.hub.runWriter(c)
	go s.book.runReader(c)
}

// handleFunding upgrades and registers the client on the funding hub.
// Same auth policy as long-short — public feed, anon clients allowed.
func (s *Service) handleFunding(w http.ResponseWriter, r *http.Request) {
	conn, err := s.upgrader.Upgrade(w, r, nil)
	if err != nil {
		log.L().Debug().Err(err).Msg("ws upgrade failed")
		return
	}
	uid, ok := s.handshakeAuth(conn, authOptional)
	if !ok {
		return
	}
	c := &client{
		uid:    uid,
		conn:   conn,
		outbox: make(chan []byte, clientOutboxSize),
		done:   make(chan struct{}),
	}
	s.funding.hub.register(c)
	if snap := s.funding.SnapshotForNewClient(); snap != nil {
		select {
		case c.outbox <- snap:
		default:
		}
	}
	go s.funding.hub.runWriter(c)
	go s.funding.hub.runReader(c)
}

// handshakeAuth reads the first text frame, validates the auth token.
// Returns (uid, true) on success or anonymous; (0, false) when the
// caller should give up (close already sent).
func (s *Service) handshakeAuth(conn *websocket.Conn, mode authMode) (int, bool) {
	_ = conn.SetReadDeadline(time.Now().Add(authReadTimeout))
	mt, data, err := conn.ReadMessage()
	if err != nil {
		if mode == authRequired {
			_ = conn.WriteMessage(websocket.CloseMessage,
				websocket.FormatCloseMessage(4401, "auth timeout"))
			_ = conn.Close()
			return 0, false
		}
		// Optional auth — connection still usable (treat as anon) only
		// if it survived the read at all. A read error here means the
		// socket is dead; bail.
		_ = conn.Close()
		return 0, false
	}
	_ = conn.SetReadDeadline(time.Time{}) // clear; runReader sets its own
	if mt != websocket.TextMessage {
		if mode == authRequired {
			_ = conn.WriteMessage(websocket.CloseMessage,
				websocket.FormatCloseMessage(4401, "auth required"))
			_ = conn.Close()
			return 0, false
		}
		return 0, true
	}
	var msg struct {
		Auth string `json:"auth"`
	}
	_ = json.Unmarshal(data, &msg)
	if msg.Auth == "" {
		if mode == authRequired {
			_ = conn.WriteMessage(websocket.CloseMessage,
				websocket.FormatCloseMessage(4401, "auth required"))
			_ = conn.Close()
			return 0, false
		}
		return 0, true
	}
	uid, err := s.jwt.Decode(msg.Auth)
	if err != nil {
		if mode == authRequired {
			_ = conn.WriteMessage(websocket.CloseMessage,
				websocket.FormatCloseMessage(4401, "invalid token"))
			_ = conn.Close()
			return 0, false
		}
		return 0, true
	}
	return uid, true
}
