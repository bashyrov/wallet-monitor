package ws

import (
	"bytes"
	"compress/gzip"
	"context"
	"errors"
	"io"
	"net/http"
	"sort"
	"sync"
	"time"

	"github.com/bytedance/sonic"
	"github.com/gorilla/websocket"
	"github.com/rs/zerolog"

	wmlog "github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
)

// staleThreshold — max time without a single inbound frame before we
// force-close and reconnect. Many edges keep TCP up but stop delivering
// (bug #20).
//
// Bumped from 30s to 90s to accommodate large prewarm sets: when the
// adapter has SubscribeDelay (HL 500ms, KuCoin 400ms) and 100-150
// symbols, the initial subscribe phase alone takes 40-75 s before the
// first data frame arrives. The previous 30s threshold killed the
// connection mid-subscribe, triggering an endless reconnect loop.
//
// Real stalls past 90s still recover (the watchdog runs every 5s, so
// recovery latency is at most threshold + 5s). The downside of the
// longer threshold is a slower hand-off when a venue genuinely freezes
// — acceptable trade because freezes are rare and the prewarm-1000 use
// case is what we're optimising for.
const staleThreshold = 90 * time.Second

// Runner owns one Adapter's connection lifecycle. One Runner per exchange.
//
// Lifecycle:
//
//	NewRunner(adapter, onUpdate) → Run(ctx)
//	  └── connect → BuildSubscribe → recv loop ─┐
//	       │                                     │ frame: parse → onUpdate
//	       │                                     │ ping:  PongFor → reply
//	       │                                     │ silent ≥30s: close, reconnect
//	       └── policy close (1008/1011/3001/4400/4401):
//	            policyBackoff.next() then retry
//	       └── transient close: transientBackoff.next() then retry
//
// Concurrency: SetSymbols() can be called from any goroutine; the runner
// serialises through symMu. The recv loop reads symbols/conn under symMu
// for the brief subscribe-window only, never on the hot path.
//
// IMPORTANT — gorilla/websocket requires "no more than one goroutine
// calls write methods concurrently". We have THREE potential writers per
// session:
//
//   1. main recv loop  — initial subscribe + ping replies
//   2. heartbeat loop  — periodic app-level pings
//   3. SetSymbols      — delta-subscribe on prewarm refresh
//
// All three go through writeMu. Without serialization Bybit/Hyperliquid
// silently drop the connection on byte-interleaved frames; the symptom
// is "WS connected" with zero data flow before reconnect.
type Runner struct {
	a        Adapter
	onUpdate UpdateFunc

	symMu      sync.Mutex
	symbols    map[string]struct{} // wanted (set by SetSymbols)
	subscribed map[string]struct{} // already-sent for current connection
	conn       *websocket.Conn

	writeMu sync.Mutex // serialises all writes to .conn
	bo      Backoff
	lastMsg atomic[time.Time]
	log     *zerolog.Logger
}

// safeSend is the ONLY sanctioned write path. Holds writeMu so the three
// concurrent writers don't interleave bytes on the wire.
func (r *Runner) safeSend(conn *websocket.Conn, payload []byte) error {
	r.writeMu.Lock()
	defer r.writeMu.Unlock()
	return SendText(conn, payload)
}

func NewRunner(a Adapter, onUpdate UpdateFunc) *Runner {
	l := wmlog.L().With().Str("exchange", a.Name()).Logger()
	return &Runner{
		a:          a,
		onUpdate:   onUpdate,
		symbols:    make(map[string]struct{}),
		subscribed: make(map[string]struct{}),
		log:        &l,
	}
}

// SetSymbols replaces the wanted set. Removed symbols force a reconnect (most
// venues have no reliable unsubscribe for batched topics — reconnecting with
// the new set is simpler than per-adapter unsub logic).
func (r *Runner) SetSymbols(syms []string) {
	r.symMu.Lock()
	wanted := make(map[string]struct{}, len(syms))
	for _, s := range syms {
		wanted[s] = struct{}{}
	}
	// detect removals
	hasRemoved := false
	for s := range r.symbols {
		if _, ok := wanted[s]; !ok {
			hasRemoved = true
			break
		}
	}
	r.symbols = wanted
	added := make([]string, 0)
	for s := range wanted {
		if _, ok := r.subscribed[s]; !ok {
			added = append(added, s)
		}
	}
	conn := r.conn
	r.symMu.Unlock()

	if hasRemoved && conn != nil {
		// Force reconnect — recv loop returns when conn closes, _run picks up new symbols.
		_ = conn.Close()
		return
	}
	if conn != nil && len(added) > 0 {
		go func() {
			if err := r.subscribe(conn, added); err != nil {
				r.log.Warn().Err(err).Msg("delta subscribe failed")
			}
		}()
	}
}

// Run blocks until ctx is cancelled. Reconnects with backoff on close.
func (r *Runner) Run(ctx context.Context) {
	for {
		if ctx.Err() != nil {
			return
		}
		if err := r.session(ctx); err != nil {
			if IsPolicyClose(err) {
				wait := r.bo.NextPolicy()
				r.log.Warn().Err(err).Dur("backoff", wait).Msg("policy close — long backoff")
				if !sleepCtx(ctx, wait) {
					return
				}
				continue
			}
			wait := r.bo.NextTransient()
			r.log.Debug().Err(err).Dur("backoff", wait).Msg("transient close")
			if !sleepCtx(ctx, wait) {
				return
			}
			continue
		}
		// Clean exit (ctx done) — return.
		return
	}
}

// session opens one connection, subscribes, runs the recv loop until the
// connection dies or ctx is cancelled. Returns the close error so Run can
// decide between transient vs policy backoff.
func (r *Runner) session(ctx context.Context) error {
	r.a.OnReconnect()

	url, err := r.a.URL(ctx)
	if err != nil {
		return err
	}

	dialer := *websocket.DefaultDialer
	dialer.HandshakeTimeout = 30 * time.Second
	dialer.EnableCompression = false // we handle gzip per-adapter when needed

	conn, _, err := dialer.DialContext(ctx, url, http.Header{
		"User-Agent": []string{"Mozilla/5.0 avalant-fetcher/go"},
	})
	if err != nil {
		return err
	}
	r.symMu.Lock()
	r.conn = conn
	r.subscribed = make(map[string]struct{})
	wantedSnap := make([]string, 0, len(r.symbols))
	for s := range r.symbols {
		wantedSnap = append(wantedSnap, s)
	}
	r.symMu.Unlock()

	defer func() {
		_ = conn.Close()
		r.symMu.Lock()
		r.conn = nil
		r.symMu.Unlock()
	}()

	r.lastMsg.Store(time.Now())
	r.bo.ResetTransient()

	// Disable lib pings if the adapter says so. gorilla doesn't expose a
	// "ping interval" — it sends pings only when we tell it to via
	// SetPongHandler / WriteControl. Default behaviour is no auto-pings,
	// so we DON'T need to do anything here for UseLibPings()==false.
	// For UseLibPings()==true we leave the default — adapters that want
	// lib pings can rely on the heartbeat goroutine below.

	if len(wantedSnap) > 0 {
		if err := r.subscribe(conn, wantedSnap); err != nil {
			return err
		}
	}

	heartbeatCtx, cancelHB := context.WithCancel(ctx)
	defer cancelHB()
	if frame := r.a.Heartbeat(); frame != nil {
		go r.heartbeatLoop(heartbeatCtx, conn, frame, r.a.HeartbeatInterval())
	}

	// Watchdog: closes the connection if we go silent for >staleThreshold.
	wdCtx, cancelWD := context.WithCancel(ctx)
	defer cancelWD()
	go r.staleWatchdog(wdCtx, conn)

	r.log.Info().Int("symbols", len(wantedSnap)).Msg("WS connected")

	frameCount := 0
	for {
		mt, raw, err := conn.ReadMessage()
		if err != nil {
			r.log.Info().Err(err).Int("frames", frameCount).Msg("ws read failed — session ending")
			if frameCount > 0 {
				return err // transient — frames flowed
			}
			// no frames at all → policy probably
			return err
		}
		r.lastMsg.Store(time.Now())
		if frameCount < 3 {
			r.log.Info().Int("mt", mt).Int("len", len(raw)).Str("preview", string(raw[:min(80, len(raw))])).Msg("recv frame")
		}

		// Decompress gzip if the adapter says so. HTX / BingX stream
		// gzip-compressed text frames.
		if r.a.DecompressGzip() && len(raw) > 0 {
			if dec, derr := gunzip(raw); derr == nil {
				raw = dec
			}
		}

		// Plain-text "ping"/"pong"/"Ping"/"Pong" frames — Bitget V2
		// replies "pong" to our app-level "ping"; KuCoin sends server
		// "ping" expecting "pong"; BingX sends "Ping" expecting "Pong".
		// All four shapes are transparent at the data-stream layer:
		// runner consumes them so Parse() doesn't see noisy bytes.
		if mt == websocket.TextMessage || mt == websocket.BinaryMessage {
			trimmed := bytes.TrimSpace(raw)
			if len(trimmed) > 0 && len(trimmed) <= 8 {
				low := bytes.ToLower(trimmed)
				if bytes.Equal(low, []byte("pong")) {
					continue
				}
				if bytes.Equal(low, []byte("ping")) {
					// adapter's PongFor decides reply (different venues
					// want different cases — runner stays neutral).
					if reply := r.a.PongFor(raw); reply != nil {
						if err := r.safeSend(conn, reply); err != nil {
							return err
						}
					}
					continue
				}
			}
			if reply := r.a.PongFor(raw); reply != nil {
				if err := r.safeSend(conn, reply); err != nil {
					return err
				}
				continue
			}
		}

		snap, perr := r.a.Parse(raw)
		if perr != nil {
			r.log.Debug().Err(perr).Msg("parse error — skipping frame")
			continue
		}
		if snap == nil {
			// non-data frame (subscribe ack, error event) — keep counting
			// time-since-last-frame as alive, but don't reset policy.
			continue
		}

		// Real data frame — both backoffs may reset.
		if frameCount == 0 {
			r.bo.ResetPolicy()
		}
		frameCount++

		// Cap each side to 200 levels — same bound as Python (more is
		// pointless for the screener UI and inflates broadcast diffs).
		if len(snap.Bids) > 200 {
			snap.Bids = snap.Bids[:200]
		}
		if len(snap.Asks) > 200 {
			snap.Asks = snap.Asks[:200]
		}
		r.onUpdate(r.a.Name(), *snap)
	}
}

// subscribe sends BuildSubscribe frames. Caller already holds wanted set.
func (r *Runner) subscribe(conn *websocket.Conn, syms []string) error {
	r.log.Info().Int("syms", len(syms)).Msg("subscribe")
	r.symMu.Lock()
	frames := r.a.BuildSubscribe(syms)
	r.symMu.Unlock()
	r.log.Info().Int("syms", len(syms)).Int("frames", len(frames)).Msg("subscribe frames built")

	delay := r.a.SubscribeDelay()
	for i, f := range frames {
		if err := r.safeSend(conn, f); err != nil {
			r.log.Warn().Err(err).Int("frame", i).Msg("subscribe send failed")
			return err
		}
		r.log.Info().Int("frame", i).Int("bytes", len(f)).Msg("subscribe frame sent")
		if delay > 0 && i < len(frames)-1 {
			time.Sleep(delay)
		}
	}
	r.symMu.Lock()
	for _, s := range syms {
		r.subscribed[s] = struct{}{}
	}
	r.symMu.Unlock()
	return nil
}

func (r *Runner) heartbeatLoop(ctx context.Context, conn *websocket.Conn, frame []byte, interval time.Duration) {
	if interval <= 0 {
		interval = 15 * time.Second
	}
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			if err := r.safeSend(conn, frame); err != nil {
				return
			}
		}
	}
}

func (r *Runner) staleWatchdog(ctx context.Context, conn *websocket.Conn) {
	t := time.NewTicker(5 * time.Second)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			last := r.lastMsg.Load()
			if last.IsZero() {
				continue
			}
			age := time.Since(last)
			if age > staleThreshold {
				r.log.Warn().Dur("age", age).Msg("WS stale — forcing reconnect")
				_ = conn.WriteControl(
					websocket.CloseMessage,
					websocket.FormatCloseMessage(websocket.CloseNormalClosure, "stale-data-watchdog"),
					time.Now().Add(time.Second),
				)
				_ = conn.Close()
				return
			}
		}
	}
}

// gunzip — decompress one gzip-encoded message. HTX/BingX use this.
func gunzip(b []byte) ([]byte, error) {
	r, err := gzip.NewReader(bytes.NewReader(b))
	if err != nil {
		return nil, err
	}
	defer r.Close()
	return io.ReadAll(r)
}

// sleepCtx blocks for d or until ctx is cancelled. Returns false when the
// caller should give up (ctx done).
func sleepCtx(ctx context.Context, d time.Duration) bool {
	if d <= 0 {
		return ctx.Err() == nil
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
}

// SortedLevels — small helper for Parse() implementers that build the book
// from a price→size dict.
//
//	side==Bids: descending price (best bid = highest)
//	side==Asks: ascending price  (best ask = lowest)
func SortedLevels(book map[float64]float64, side Side, cap int) []Level {
	out := make([]Level, 0, len(book))
	for px, sz := range book {
		if sz <= 0 {
			continue
		}
		out = append(out, Level{px, sz})
	}
	if side == Bids {
		sort.Slice(out, func(i, j int) bool { return out[i][0] > out[j][0] })
	} else {
		sort.Slice(out, func(i, j int) bool { return out[i][0] < out[j][0] })
	}
	if cap > 0 && len(out) > cap {
		out = out[:cap]
	}
	return out
}

// JSON helpers used by adapters — keep here so adapters can be slim.

// MarshalJSON wraps sonic.Marshal — sonic is 3-4× faster than encoding/json
// on the marshal side too. Returns []byte (not string) — pair with
// SendText().
func MarshalJSON(v any) ([]byte, error) {
	return sonic.Marshal(v)
}

// UnmarshalJSON wraps sonic.Unmarshal.
func UnmarshalJSON(data []byte, v any) error {
	return sonic.Unmarshal(data, v)
}

// IsClosed reports whether the error means the underlying conn is gone.
func IsClosed(err error) bool {
	if err == nil {
		return false
	}
	if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
		return true
	}
	if _, ok := err.(*websocket.CloseError); ok {
		return true
	}
	return false
}

// atomic[T] — tiny generic for time.Time stored under sync.Mutex. Go's
// sync/atomic doesn't yet have a clean atomic.Time; sub-millisecond accuracy
// is fine for our use (watchdog runs every 5s).
type atomic[T any] struct {
	mu sync.Mutex
	v  T
}

func (a *atomic[T]) Load() T {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.v
}

func (a *atomic[T]) Store(v T) {
	a.mu.Lock()
	a.v = v
	a.mu.Unlock()
}
