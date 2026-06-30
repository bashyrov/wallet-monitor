package funding

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

// Runner owns one Adapter's lifecycle: WS reconnect loop + REST backstop
// goroutine. The two paths share the Store via Apply() — WS ticks fill
// in fast (rate, mark price), REST backstop fills in heavy fields
// (volume, open interest) that WS pushes often omit.
type Runner struct {
	a       Adapter
	store   *Store
	syms    []string
	symsMu  sync.RWMutex
	writeMu sync.Mutex // serialises all writes to the live conn
	log     *zerolog.Logger

	// lastWS — wall-clock of the last WS-driven Store.Apply for this
	// venue. The REST backstop uses this to skip its tick when WS is
	// fresh: there's no point hitting REST every 2s when the WS is
	// pushing every <500ms. Only restored on actual data, not pings.
	lastWS   time.Time
	lastWSMu sync.RWMutex

	// lastREST — wall-clock of the last successful REST sweep. The
	// adaptive WS-skip guards against unbounded skipping: even if WS
	// stays fresh forever, REST still runs at least every restMaxSkip.
	// This matters because most adapters' WS payloads are partial
	// (rate + mark only) and REST is the only source for NextFunding /
	// IntervalH / OpenInterest. Without this Gate's next_funding got
	// stuck on the previous period after every settlement.
	lastREST   time.Time
	lastRESTMu sync.RWMutex
}

// Maximum time the REST sweep is allowed to be skipped in favour of
// WS freshness. Past this, REST must run regardless — most WS payloads
// don't carry NextFunding/IntervalH so stale next_funding accumulates
// silently otherwise.
const restMaxSkip = 30 * time.Second

func NewRunner(a Adapter, store *Store) *Runner {
	l := wmlog.L().With().Str("funding", a.Name()).Logger()
	return &Runner{a: a, store: store, log: &l}
}

func (r *Runner) markWSAlive() {
	r.lastWSMu.Lock()
	r.lastWS = time.Now()
	r.lastWSMu.Unlock()
}

func (r *Runner) wsFreshFor(d time.Duration) bool {
	r.lastWSMu.RLock()
	last := r.lastWS
	r.lastWSMu.RUnlock()
	if last.IsZero() {
		return false
	}
	return time.Since(last) < d
}

func (r *Runner) markRESTRun() {
	r.lastRESTMu.Lock()
	r.lastREST = time.Now()
	r.lastRESTMu.Unlock()
}

func (r *Runner) restAge() time.Duration {
	r.lastRESTMu.RLock()
	last := r.lastREST
	r.lastRESTMu.RUnlock()
	if last.IsZero() {
		return time.Hour // treat as ancient — first tick will run
	}
	return time.Since(last)
}

// safeSend — single sanctioned write path. Holds writeMu so the
// recv-loop (subscribes, ping replies) and the heartbeat goroutine
// don't byte-interleave on the same conn (gorilla/websocket contract:
// only one concurrent writer).
func (r *Runner) safeSend(conn *websocket.Conn, payload []byte) error {
	r.writeMu.Lock()
	defer r.writeMu.Unlock()
	return ws.SendText(conn, payload)
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
			if err := r.safeSend(conn, frame); err != nil {
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
				if err := r.safeSend(conn, reply); err != nil {
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
		if len(ticks) > 0 {
			r.markWSAlive()
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
			// Adaptive: skip the REST sweep when WS has pushed any data
			// in the last 5 seconds — REST exists as safety net, no
			// point burning RTT every 2s if WS is delivering.
			//
			// BUT: most adapters' WS payloads are partial. Gate/Bybit/
			// Binance WS carry rate+mark+volume but NOT NextFunding or
			// FundingInterval. If WS stays fresh forever, REST never
			// runs, and after settlement the cached NextFunding stays
			// stuck on the previous period (one period = 1-8h of
			// staleness on a user-visible field). restMaxSkip caps the
			// skip window so REST refreshes at least every 30s.
			if r.wsFreshFor(5*time.Second) && r.restAge() < restMaxSkip {
				continue
			}
			r.runBackstopOnce(ctx)
		}
	}
}

func (r *Runner) runBackstopOnce(ctx context.Context) {
	syms := r.symbols()
	// Per-call deadline. 10s is fine for venues with bulk endpoints
	// (binance, bitget, gate — single REST call), but OKX needs
	// per-symbol funding-rate fetches (no bulk equivalent in V5),
	// and 318 symbols × 8 parallel × ~500ms hits the cap.
	// Stretch the deadline up to the BackstopInterval — 1s so the
	// next tick still has room to start — with a 5-60s clamp.
	timeout := r.a.BackstopInterval() - time.Second
	if timeout < 10*time.Second {
		timeout = 10 * time.Second
	}
	if timeout > 60*time.Second {
		timeout = 60 * time.Second
	}
	cctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	ticks, err := r.a.BackstopFetch(cctx, syms)
	if err != nil {
		r.log.Debug().Err(err).Msg("rest backstop failed")
		return
	}
	for _, t := range ticks {
		r.store.Apply(r.a.Name(), t)
	}
	r.markRESTRun()
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
			if err := r.safeSend(conn, frame); err != nil {
				return
			}
		}
	}
}

func gunzip(data []byte) ([]byte, error) {
	rdr, err := gzip.NewReader(bytes.NewReader(data))
	if err != nil {
		return nil, err
	}
	defer rdr.Close()
	return io.ReadAll(rdr)
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

// httpClient — process-wide HTTP/1.1+keepalive client used by all REST
// backstops. Single Transport keeps idle connections warm so successive
// per-symbol REST calls (e.g. OKX funding-rate sweep, BingX userTrades
// per-symbol) reuse the same TCP+TLS instead of re-handshaking each.
//
// HTTP/2 not enabled: most exchange APIs that we hit (binance fapi, bybit,
// okx) accept HTTP/2 but their CDN servers (Cloudfront, Akamai) sometimes
// limit per-stream throughput in ways that hurt parallel-fanout latency.
// HTTP/1.1 with a 100-conn pool gives more predictable behaviour.
var httpClient = &http.Client{
	Timeout: 8 * time.Second,
	Transport: &http.Transport{
		MaxIdleConns:        100,
		MaxIdleConnsPerHost: 50,
		IdleConnTimeout:     300 * time.Second,
		// Disable per-request connection cap; the per-host idle pool above
		// handles burst control.
		DisableCompression: false,
		ForceAttemptHTTP2:  true, // upgrade where venue supports it
	},
}

// HTTPGet is a convenience used by REST backstops. Reuses a process-wide
// client so successive calls reuse warm TCP+TLS connections (was paying
// a fresh handshake per call).
func HTTPGet(ctx context.Context, url string, out any) error {
	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return err
	}
	req.Header.Set("User-Agent", "Mozilla/5.0 avalant-fetcher/go")
	resp, err := httpClient.Do(req)
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
