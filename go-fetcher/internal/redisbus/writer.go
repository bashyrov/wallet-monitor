package redisbus

import (
	"context"
	"strconv"
	"sync"
	"time"

	"github.com/bytedance/sonic"
	"github.com/redis/go-redis/v9"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

// Writer mirrors Python's orderbook_redis.py write path: per-key SETEX
// at `ob:<exchange>:<symbol>` with the JSON shape:
//
//	{"ts": <epoch_seconds_float>, "data": {"bids": [[px,sz],...], "asks": [...]}}
//
// TTL 10s — Python uses the same value. Per-key throttle (default 50ms)
// avoids burning Redis CPU on chatty venues without sacrificing
// freshness for the read path.
type Writer struct {
	client   *redis.Client
	throttle time.Duration

	mu        sync.Mutex
	lastWrite map[string]time.Time
}

const obTTL = 10 * time.Second

func NewWriter(redisURL string, throttle time.Duration) (*Writer, error) {
	if redisURL == "" {
		return nil, nil
	}
	opts, err := redis.ParseURL(redisURL)
	if err != nil {
		return nil, err
	}
	if throttle <= 0 {
		throttle = 50 * time.Millisecond
	}
	return &Writer{
		client:    redis.NewClient(opts),
		throttle:  throttle,
		lastWrite: make(map[string]time.Time, 1024),
	}, nil
}

// WriteBook publishes a book update for one (exchange, symbol). Cheap —
// per-key rate-limited, async-ish (single round-trip, no blocking
// caller's recv loop on Redis latency). Errors logged, never returned.
func (w *Writer) WriteBook(exchange, symbol string, bids, asks []ws.Level) {
	if w == nil {
		return
	}
	key := "ob:" + exchange + ":" + symbol

	w.mu.Lock()
	now := time.Now()
	if last, ok := w.lastWrite[key]; ok && now.Sub(last) < w.throttle {
		w.mu.Unlock()
		return
	}
	w.lastWrite[key] = now
	w.mu.Unlock()

	// Python's shape — keep literal compatibility, including the float
	// epoch (not millis like our other timestamps).
	tsFloat := float64(now.UnixMilli()) / 1000.0
	payload := map[string]any{
		"ts":   tsFloat,
		"data": map[string]any{"bids": bids, "asks": asks},
	}
	body, err := sonic.Marshal(payload)
	if err != nil {
		log.L().Debug().Err(err).Str("key", key).Msg("redis write marshal")
		return
	}

	// 1s overall write timeout — if Redis is slow we'd rather skip than
	// block the recv loop on the orderbook adapter.
	ctx, cancel := context.WithTimeout(context.Background(), 1*time.Second)
	defer cancel()
	if err := w.client.Set(ctx, key, body, obTTL).Err(); err != nil {
		log.L().Debug().Err(err).Str("key", key).Msg("redis SETEX failed")
	}
}

// FloatTS — convert time.Time to Python-style epoch seconds float, used
// for parity with Python's books.json/funding.json timestamps.
func FloatTS(t time.Time) string {
	return strconv.FormatFloat(float64(t.UnixMilli())/1000.0, 'f', 6, 64)
}

func (w *Writer) Close() error {
	if w == nil {
		return nil
	}
	return w.client.Close()
}
