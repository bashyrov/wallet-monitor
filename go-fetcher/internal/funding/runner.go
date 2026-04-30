package funding

import (
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

// Runner owns one Adapter's lifecycle: WS reconnect loop + REST backstop
// goroutine. The two paths share the Store via Apply() — WS ticks fill
// in fast (rate, mark price), REST backstop fills in heavy fields
// (volume, open interest) that WS pushes often omit.
type Runner struct {
	a        Adapter
	store    *Store
	syms     []string
	symsMu   sync.RWMutex
	log      *zerolog.Logger
}

func NewRunner(a Adapter, store *Store) *Runner {
	l := wmlog.L().With().Str("funding", a.Name()).Logger()
	return &Runner{a: a, store: store, log: &l}
}

// SetSymbols replaces the wanted set. Both WS and REST goroutines see the
// new list within their next iteration.
func (r *Runner) SetSymbols(syms []string) {
	r.symsMu.Lock()
	r.syms = append([]string(nil), syms...)
	r.symsMu.Unlock()
}

func (r *Runner) symbols() []string {
	r.symsMu.RLock()
	defer r.symsMu.RUnlock()
	return append([]string(nil), r.syms...)
}

// Run blocks until ctx is cancelled. Spawns:
//
//	wsLoop  — reconnect-with-backoff WS subscriber. Skipped if URL == "".
//	restLoop — periodic REST backstop. Always on.
func (r *Runner) Run(ctx context.Context) {
	var wg sync.WaitGroup
	if hasWS := r.adapterHasWS(ctx); hasWS {
		wg.Add(1)
		go func() {
			defer wg.Done()
			r.wsLoop(ctx)
		}()
	}
	wg.Add(1)
	go func() {
		defer wg.Done()
		r.restLoop(ctx)
	}()
	wg.Wait()
}

// adapterHasWS returns true if URL() yields a non-empty string. We probe
// once at startup so REST-only adapters don't burn cycles in wsLoop.
func (r *Runner) adapterHasWS(ctx context.Context) bool {
	u, err := r.a.URL(ctx)
	return err == nil && u != ""
}

func (r *Runner) wsLoop(ctx context.Context) {
	transient := 300 * time.Millisecond
	policy := 30 * time.Second
	const transientCap = 30 * time.Second
	const policyCap = 5 * time.Minute

	for {
		if ctx.Err() != nil {
			return
		}
		err := r.wsSession(ctx)
		if err == nil {
			return
		}
		// Same policy-vs-transient split as orderbook ws (bugs #2, #3).
		if isPolicyClose(err) {
			r.log.Warn().Err(err).Dur("backoff", policy).Msg("WS policy close")
			if !sleepCtx(ctx, policy) {
				return
			}
			policy *= 2
			if policy > policyCap {
				policy = policyCap
			}
			continue
		}
		r.log.Debug().Err(err).Dur("backoff", transient).Msg("WS transient close")
		if !sleepCtx(ctx, transient) {
			return
		}
		transient *= 2
		if transient > transientCap {
			transient = transientCap
		}
	}
}

func (r *Runner) wsSession(ctx context.Context) error {
	url, err := r.a.URL(ctx)
	if err != nil {
		return err
	}
	if url == "" {
		// runner shouldn't have entered wsLoop; defensive return.
		return errors.New("ws disabled")
	}

	dialer := *websocket.DefaultDialer
	dialer.HandshakeTimeout = 30 * time.Second
	dialer.EnableCompression = false

	conn, _, err := dialer.DialContext(ctx, url, http.Header{
		"User-Agent": []string{"Mozilla/5.0 avalant-fetcher/go"},
	})
	if err != nil {
		return err
	}
	defer conn.Close()

	syms := r.symbols()
	if len(syms) > 0 {
		for _, frame := range r.a.BuildSubscribe(syms) {
			if err := ws.SendText(conn, frame); err != nil {
				return err
			}
		}
	}

	hbCtx, hbCancel := context.WithCancel(ctx)
	defer hbCancel()
	if hb := r.a.Heartbeat(); hb != nil {
		go r.heartbeat(hbCtx, conn, hb, r.a.HeartbeatInterval())
	}

	r.log.Info().Int("symbols", len(syms)).Msg("funding WS connected")

	for {
		mt, raw, err := conn.ReadMessage()
		if err != nil {
			return err
		}
		if r.a.DecompressGzip() && len(raw) > 0 {
			if dec, derr := gunzip(raw); derr == nil {
				raw = dec
			}
		}
		if mt == websocket.TextMessage || mt == websocket.BinaryMessage {
			if reply := r.a.PongFor(raw); reply != nil {
				if err := ws.SendText(conn, reply); err != nil {
					return err
				}
				continue
			}
		}
		ticks, perr := r.a.ParseWS(raw)
		if perr != nil {
			r.log.Debug().Err(perr).Msg("ws parse error")
			continue
		}
		for _, t := range ticks {
			r.store.Apply(r.a.Name(), t)
		}
	}
}

func (r *Runner) restLoop(ctx context.Context) {
	interval := r.a.BackstopInterval()
	if interval <= 0 {
		interval = 2 * time.Second
	}
	// Tiny stagger to avoid all 12 venues hitting REST in lockstep — the
	// jitter helps spread our outbound burst.
	stagger := time.Duration(int64(interval) / 12)
	time.Sleep(stagger)

	t := time.NewTicker(interval)
	defer t.Stop()

	r.runBackstopOnce(ctx) // immediate first sweep — no waiting on first tick
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			r.runBackstopOnce(ctx)
		}
	}
}

func (r *Runner) runBackstopOnce(ctx context.Context) {
	syms := r.symbols()
	cctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	ticks, err := r.a.BackstopFetch(cctx, syms)
	if err != nil {
		r.log.Debug().Err(err).Msg("rest backstop failed")
		return
	}
	for _, t := range ticks {
		r.store.Apply(r.a.Name(), t)
	}
	r.log.Debug().Int("ticks", len(ticks)).Msg("rest backstop ok")
}

// ── helpers ──────────────────────────────────────────────────────────────

func (r *Runner) heartbeat(ctx context.Context, conn *websocket.Conn, frame []byte, interval time.Duration) {
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
			if err := ws.SendText(conn, frame); err != nil {
				return
			}
		}
	}
}

func gunzip(data []byte) ([]byte, error) {
	rdr, err := gzip.NewReader(byteReader(data))
	if err != nil {
		return nil, err
	}
	defer rdr.Close()
	return io.ReadAll(rdr)
}

type byteReader []byte

func (b byteReader) Read(p []byte) (int, error) {
	n := copy(p, b)
	if n == 0 {
		return 0, io.EOF
	}
	return n, nil
}

func isPolicyClose(err error) bool {
	var ce *websocket.CloseError
	if !errors.As(err, &ce) {
		return false
	}
	switch ce.Code {
	case 1008, 1011, 3001, 4400, 4401:
		return true
	}
	return false
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

// HTTPGet is a convenience used by REST backstops. Times out per-call so
// a hung venue doesn't pin the runner.
func HTTPGet(ctx context.Context, url string, out any) error {
	cl := &http.Client{Timeout: 8 * time.Second}
	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return err
	}
	req.Header.Set("User-Agent", "Mozilla/5.0 avalant-fetcher/go")
	resp, err := cl.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return err
	}
	if resp.StatusCode != 200 {
		return errors.New("http " + resp.Status)
	}
	return sonic.Unmarshal(body, out)
}
