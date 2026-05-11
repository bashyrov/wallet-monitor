// runner.go — trade-stream WS runner.
//
// Architecture-wise identical to internal/ws/runner.go (connection
// lifecycle, reconnect with backoff, heartbeat dispatch, stale-frames
// watchdog, gzip decompression). The only difference is the Parse
// callback returns []Tick instead of *Snapshot.
//
// We intentionally duplicate ~150 lines rather than refactor the ws
// package to be generic — the duplication is cheap and isolating the
// trade-stream path means tweaks (e.g. trade-stream-specific backoff)
// don't ripple into the orderbook hot path.
package ticks

import (
	"bytes"
	"compress/gzip"
	"context"
	"errors"
	"io"
	"net/http"
	"sync"
	"time"

	"github.com/bytedance/sonic"
	"github.com/gorilla/websocket"
	"github.com/rs/zerolog"

	wmlog "github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

var gzipPool = sync.Pool{New: func() any { return (*gzip.Reader)(nil) }}

const staleThreshold = 90 * time.Second

// Runner owns one trade-stream Adapter's connection. One per venue.
type Runner struct {
	a        Adapter
	onUpdate UpdateFunc

	symMu      sync.Mutex
	symbols    map[string]struct{}
	subscribed map[string]struct{}
	conn       *websocket.Conn

	writeMu sync.Mutex
	bo      ws.Backoff
	lastMsg atomicTime
	log     *zerolog.Logger
}

func NewRunner(a Adapter, onUpdate UpdateFunc) *Runner {
	l := wmlog.L().With().Str("exchange", a.Name()).Str("stream", "ticks").Logger()
	return &Runner{
		a:          a,
		onUpdate:   onUpdate,
		symbols:    make(map[string]struct{}),
		subscribed: make(map[string]struct{}),
		log:        &l,
	}
}

func (r *Runner) safeSend(conn *websocket.Conn, payload []byte) error {
	r.writeMu.Lock()
	defer r.writeMu.Unlock()
	return ws.SendText(conn, payload)
}

// SetSymbols mirrors ws.Runner.SetSymbols semantics. Removed symbols
// force a reconnect; added symbols get a delta-subscribe.
func (r *Runner) SetSymbols(syms []string) {
	r.symMu.Lock()
	wanted := make(map[string]struct{}, len(syms))
	for _, s := range syms {
		wanted[s] = struct{}{}
	}
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

func (r *Runner) Run(ctx context.Context) {
	for {
		if ctx.Err() != nil {
			return
		}
		if err := r.session(ctx); err != nil {
			if ws.IsPolicyClose(err) {
				wait := r.bo.NextPolicy()
				r.log.Warn().Err(err).Dur("backoff", wait).Msg("policy close")
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
		return
	}
}

func (r *Runner) session(ctx context.Context) error {
	r.a.OnReconnect()

	url, err := r.a.URL(ctx)
	if err != nil {
		return err
	}

	dialer := *websocket.DefaultDialer
	dialer.HandshakeTimeout = 30 * time.Second
	dialer.EnableCompression = false

	conn, _, err := dialer.DialContext(ctx, url, http.Header{
		"User-Agent": []string{"Mozilla/5.0 avalant-fetcher/go-ticks"},
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

	if len(wantedSnap) > 0 {
		if err := r.subscribe(conn, wantedSnap); err != nil {
			return err
		}
	}

	hbCtx, cancelHB := context.WithCancel(ctx)
	defer cancelHB()
	if frame := r.a.Heartbeat(); frame != nil {
		go r.heartbeatLoop(hbCtx, conn, frame, r.a.HeartbeatInterval())
	}

	wdCtx, cancelWD := context.WithCancel(ctx)
	defer cancelWD()
	go r.staleWatchdog(wdCtx, conn)

	r.log.Info().Int("symbols", len(wantedSnap)).Msg("ticks WS connected")

	frameCount := 0
	for {
		mt, raw, err := conn.ReadMessage()
		if err != nil {
			r.log.Info().Err(err).Int("frames", frameCount).Msg("ticks ws read failed")
			return err
		}
		r.lastMsg.Store(time.Now())

		if r.a.DecompressGzip() && len(raw) > 0 {
			if dec, derr := gunzip(raw); derr == nil {
				raw = dec
			}
		}

		// transparent ping/pong handling — same shape as orderbook runner
		if mt == websocket.TextMessage || mt == websocket.BinaryMessage {
			trimmed := bytes.TrimSpace(raw)
			if len(trimmed) > 0 && len(trimmed) <= 8 {
				low := bytes.ToLower(trimmed)
				if bytes.Equal(low, []byte("pong")) {
					continue
				}
				if bytes.Equal(low, []byte("ping")) {
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

		ticks, perr := r.a.Parse(raw)
		if perr != nil {
			r.log.Debug().Err(perr).Msg("parse error")
			continue
		}
		if len(ticks) == 0 {
			continue
		}
		if frameCount == 0 {
			r.bo.ResetPolicy()
		}
		frameCount++
		for _, t := range ticks {
			r.onUpdate(t)
		}
	}
}

func (r *Runner) subscribe(conn *websocket.Conn, syms []string) error {
	if max := r.a.MaxSymbols(); max > 0 && len(syms) > max {
		r.log.Warn().Int("syms", len(syms)).Int("max", max).Msg("ticks subscribe truncated to MaxSymbols")
		syms = syms[:max]
	}
	r.symMu.Lock()
	frames := r.a.BuildSubscribe(syms)
	r.symMu.Unlock()
	r.log.Info().Int("syms", len(syms)).Int("frames", len(frames)).Msg("ticks subscribe")

	delay := r.a.SubscribeDelay()
	for i, f := range frames {
		if err := r.safeSend(conn, f); err != nil {
			return err
		}
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
			if age := time.Since(last); age > staleThreshold {
				r.log.Warn().Dur("age", age).Msg("ticks WS stale — reconnect")
				_ = conn.WriteControl(
					websocket.CloseMessage,
					websocket.FormatCloseMessage(websocket.CloseNormalClosure, "stale-ticks-watchdog"),
					time.Now().Add(time.Second),
				)
				_ = conn.Close()
				return
			}
		}
	}
}

func gunzip(b []byte) ([]byte, error) {
	r, _ := gzipPool.Get().(*gzip.Reader)
	br := bytes.NewReader(b)
	if r == nil {
		var err error
		r, err = gzip.NewReader(br)
		if err != nil {
			return nil, err
		}
	} else {
		if err := r.Reset(br); err != nil {
			gzipPool.Put(r)
			return nil, err
		}
	}
	out, err := io.ReadAll(r)
	_ = r.Close()
	gzipPool.Put(r)
	return out, err
}

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

// MarshalJSON / UnmarshalJSON re-exported for adapters' convenience.
func MarshalJSON(v any) ([]byte, error)   { return sonic.Marshal(v) }
func UnmarshalJSON(b []byte, v any) error { return sonic.Unmarshal(b, v) }

// IsClosed for adapters checking error context.
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

type atomicTime struct {
	mu sync.Mutex
	v  time.Time
}

func (a *atomicTime) Load() time.Time {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.v
}

func (a *atomicTime) Store(v time.Time) {
	a.mu.Lock()
	a.v = v
	a.mu.Unlock()
}
