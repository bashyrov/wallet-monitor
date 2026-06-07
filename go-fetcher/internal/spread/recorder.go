// Package spread aggregates in/out spread observations from the arb
// compute tick into 5-second OHLC candles and ships them to a Redis
// stream for the Python consumer to batch-insert into PostgreSQL.
//
// Behind feature flag AVALANT_SPREAD_HISTORY=1 — Recorder returns a
// no-op stub when the flag is off so the existing arb compute path
// pays zero cost (one branch per RecordOpp call, no allocation).
//
// Design notes:
//   - In-memory bucket map keyed by (long_ex, short_ex, symbol, bucket_ts)
//     accumulates open/high/low/close + samples count for each 5s window.
//   - Flush goroutine runs every 5 seconds, drains all buckets whose
//     bucket_ts < currentBucketTs (i.e. window already closed), and
//     publishes them to Redis stream `arb:spread:bucket` via XADD.
//   - Each XADD writes one JSON blob per bucket. Stream max-len capped
//     so a dead consumer can't OOM Redis (~1M entries = ~2 hours of
//     500-pair × 12-flush/min volume).
//
// Known gap: on go-fetcher restart, the in-flight bucket (whichever 5s
// window was open at SIGTERM time) is lost. ON CONFLICT recovery in
// the consumer handles partial writes; it does NOT reconstruct lost
// buckets. The chart renders a whitespace gap there — honest, not a
// fake connected line through missing data.
package spread

import (
	"context"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/bytedance/sonic"
	"github.com/redis/go-redis/v9"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
)

// bucketSec is the candle width in seconds. 5s is the smallest unit
// fed to the chart; 1m and 1h roll up server-side from this stream.
const bucketSec int64 = 5

// streamName is the Redis stream the consumer XREAD-s. Same constant
// duplicated in Python — keep in sync if renamed.
const streamName = "arb:spread:bucket"

// streamMaxLen bounds Redis memory: ~1M entries → ~120 MB at 120 bytes
// per JSON blob. Two hours of 6000-write/min volume. Far beyond the
// expected consumer lag; any longer and the right fix is alerting on
// XLEN, not stretching the buffer.
const streamMaxLen int64 = 1_000_000

// flushEvery is how often the flush goroutine drains completed buckets.
// 5s — same as bucketSec — guarantees a bucket waits at most ~10s in
// memory before publishing.
const flushEvery = 5 * time.Second

// Bucket is one (long_ex, short_ex, symbol, bucket_ts) OHLC slot.
type Bucket struct {
	ExL, ExS, Sym string
	BucketTs      int64

	InO, InH, InL, InC     float64
	OutO, OutH, OutL, OutC float64
	Samples                int
}

// Recorder buffers buckets in-memory and flushes them to the Redis
// stream on a 5s cadence. The zero value is the disabled no-op state
// (Enabled=false) so the call sites can call RecordOpp unconditionally.
type Recorder struct {
	Enabled bool

	client *redis.Client
	topN   int

	mu      sync.Mutex
	buckets map[string]*Bucket // key: "exL|exS|sym|bucketTs"
}

// New constructs a recorder. Reads AVALANT_SPREAD_HISTORY env at boot;
// returns a disabled (no-op) recorder when the flag is off or no
// Redis URL is configured. Always returns non-nil so callers don't
// need a nil check.
func New(redisURL string) *Recorder {
	if strings.TrimSpace(os.Getenv("AVALANT_SPREAD_HISTORY")) != "1" {
		return &Recorder{Enabled: false}
	}
	if redisURL == "" {
		log.L().Warn().Msg("spread recorder: AVALANT_SPREAD_HISTORY=1 but REDIS_URL empty — disabled")
		return &Recorder{Enabled: false}
	}
	opts, err := redis.ParseURL(redisURL)
	if err != nil {
		log.L().Warn().Err(err).Msg("spread recorder: bad REDIS_URL — disabled")
		return &Recorder{Enabled: false}
	}
	topN := 500 // env-overridable below
	if s := strings.TrimSpace(os.Getenv("AVALANT_SPREAD_HISTORY_TOPN")); s != "" {
		if v, ok := parseInt(s); ok && v > 0 && v <= 2000 {
			topN = v
		}
	}
	r := &Recorder{
		Enabled: true,
		client:  redis.NewClient(opts),
		topN:    topN,
		buckets: make(map[string]*Bucket, 4096),
	}
	log.L().Info().Int("topN", topN).Msg("spread recorder ENABLED")
	return r
}

// TopN returns how many top-ranked pairs the recorder will accept. The
// caller (arb compute) iterates its already-sorted opps slice and stops
// at this index — pairs outside top-N never reach the recorder so the
// in-memory map stays bounded.
//
// A pair can fall out of top-N between flushes; the buckets it already
// produced stay in memory until flushed (correct — retention is by
// date, not by current ranking).
func (r *Recorder) TopN() int {
	if r == nil || !r.Enabled {
		return 0
	}
	return r.topN
}

// RecordOpp ingests one observation. Called once per opp on every arb
// compute tick (currently 200ms = 5 Hz). Safe to call from the
// compute goroutine; lock contention is per-Recorder, not per-bucket.
//
// No-op when the recorder is disabled or any of the required fields
// are missing.
func (r *Recorder) RecordOpp(longEx, shortEx, symbol string, inPct, outPct float64, now time.Time) {
	if r == nil || !r.Enabled {
		return
	}
	if longEx == "" || shortEx == "" || symbol == "" {
		return
	}
	// Floor to 5s window boundary. Bucket key includes ts so simultaneous
	// observations on the same pair across the window boundary land in
	// different buckets (correct OHLC).
	bts := (now.Unix() / bucketSec) * bucketSec
	key := longEx + "|" + shortEx + "|" + symbol + "|" + formatInt(bts)

	r.mu.Lock()
	b, ok := r.buckets[key]
	if !ok {
		b = &Bucket{
			ExL: longEx, ExS: shortEx, Sym: symbol, BucketTs: bts,
			InO: inPct, InH: inPct, InL: inPct, InC: inPct,
			OutO: outPct, OutH: outPct, OutL: outPct, OutC: outPct,
			Samples: 1,
		}
		r.buckets[key] = b
		r.mu.Unlock()
		return
	}
	// Existing bucket — update high/low/close, keep open (first observation
	// in this window) immutable.
	if inPct > b.InH {
		b.InH = inPct
	}
	if inPct < b.InL {
		b.InL = inPct
	}
	b.InC = inPct
	if outPct > b.OutH {
		b.OutH = outPct
	}
	if outPct < b.OutL {
		b.OutL = outPct
	}
	b.OutC = outPct
	b.Samples++
	r.mu.Unlock()
}

// Run starts the flush loop. Blocks until ctx is cancelled. Recorder
// must be enabled — call sites guard this themselves.
func (r *Recorder) Run(ctx context.Context) error {
	if r == nil || !r.Enabled {
		<-ctx.Done()
		return nil
	}
	t := time.NewTicker(flushEvery)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			// Best-effort final flush — drain any completed buckets and
			// publish them before shutdown. In-flight (current window)
			// buckets are still lost; that's the documented gap.
			r.flushOnce(context.Background())
			return nil
		case <-t.C:
			r.flushOnce(ctx)
		}
	}
}

// flushOnce drains buckets whose window has already closed and XADDs
// them to the stream. Buckets for the current window stay in memory
// until the next tick.
func (r *Recorder) flushOnce(ctx context.Context) {
	currentBucket := (time.Now().Unix() / bucketSec) * bucketSec

	r.mu.Lock()
	completed := make([]*Bucket, 0, len(r.buckets))
	for k, b := range r.buckets {
		if b.BucketTs < currentBucket {
			completed = append(completed, b)
			delete(r.buckets, k)
		}
	}
	r.mu.Unlock()

	if len(completed) == 0 {
		return
	}
	for _, b := range completed {
		payload, err := sonic.Marshal(map[string]any{
			"el":   b.ExL,
			"es":   b.ExS,
			"sym":  b.Sym,
			"ts":   b.BucketTs,
			"io":   round4(b.InO),
			"ih":   round4(b.InH),
			"il":   round4(b.InL),
			"ic":   round4(b.InC),
			"oo":   round4(b.OutO),
			"oh":   round4(b.OutH),
			"ol":   round4(b.OutL),
			"oc":   round4(b.OutC),
			"n":    b.Samples,
		})
		if err != nil {
			continue
		}
		// MaxLen with ~ (approximate trimming) is much cheaper than
		// strict trim — Redis can keep the stream a bit over the cap
		// to avoid expensive O(n) prunes on every write.
		if err := r.client.XAdd(ctx, &redis.XAddArgs{
			Stream: streamName,
			MaxLen: streamMaxLen,
			Approx: true,
			Values: map[string]any{"d": payload},
		}).Err(); err != nil {
			log.L().Warn().Err(err).Int("n", len(completed)).Msg("spread XADD failed")
			// Drop the rest of this batch on persistent failure — we don't
			// want backpressure to grow unbounded. Next flush retries with
			// fresh buckets.
			return
		}
	}
}

func parseInt(s string) (int, bool) {
	n := 0
	for _, c := range s {
		if c < '0' || c > '9' {
			return 0, false
		}
		n = n*10 + int(c-'0')
	}
	return n, true
}

func formatInt(n int64) string {
	if n == 0 {
		return "0"
	}
	neg := false
	if n < 0 {
		neg = true
		n = -n
	}
	buf := [20]byte{}
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	if neg {
		i--
		buf[i] = '-'
	}
	return string(buf[i:])
}

func round4(v float64) float64 {
	if v != v { // NaN
		return 0
	}
	// 4 decimal places — same precision the chart shows ("+0.0001%" etc.).
	const m = 10000.0
	return float64(int64(v*m+sign05(v))) / m
}

func sign05(v float64) float64 {
	if v < 0 {
		return -0.5
	}
	return 0.5
}
