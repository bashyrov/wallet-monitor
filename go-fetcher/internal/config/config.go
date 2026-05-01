// Package config loads runtime config from env-vars. Names match Python so
// the same .env on prod can be reused — just point the new container at it.
package config

import (
	"os"
	"strconv"
	"strings"
	"time"
)

type Config struct {
	// File-cache root — where books.<ex>.json, books.master.json,
	// funding.json, spot_arbitrage.json live. Bind-mounted into Python
	// containers as /tmp/avalant_cache too, so writes from Go are
	// immediately visible to Python web roles.
	CacheDir string

	// Redis is optional — used for cross-process subscribe channel
	// (book:subscribe / book:unsubscribe) and for fast per-key
	// orderbook reads from web roles. Without it, the web→fetcher
	// path falls back to file-only.
	RedisURL string

	// Per-key Redis-write throttle. Default 50ms (20 Hz) — same as
	// Python's _REDIS_MIN_INTERVAL_S. Higher = less Redis CPU,
	// slower client-side latency.
	RedisWriteThrottle time.Duration

	// File-dump cadence. Python merger runs at 100ms; we match.
	FileDumpInterval time.Duration

	// Prewarm cap — top N hot pairs subscribed pre-emptively per
	// exchange. Same env-var Python reads.
	PrewarmTopN int

	// Per-symbol idle timeout. After this many seconds of no
	// last_request bumps, the orderbook poller stops. Matches Python
	// IDLE_TIMEOUT.
	IdleTimeout time.Duration

	// Comma-separated exchange enable list. Empty = all. Useful for
	// per-replica sharding (one container subscribes Binance + OKX
	// only, another runs the rest).
	WorkerExchanges []string

	// Log level
	LogLevel string

	// WS broadcaster HTTP listen port. Empty = disabled (Python keeps
	// owning /api/screener/ws/*). Default "8090" for prod where nginx
	// is configured to upstream the path family at this port.
	WSBroadcastPort string
}

// Load reads env-vars and applies defaults. Never returns error — missing
// vars use sensible defaults so the dev workflow is `go run` with no env.
func Load() Config {
	return Config{
		CacheDir:           getenv("AVALANT_FETCHER_CACHE_DIR", "/tmp/avalant_cache"),
		RedisURL:           getenv("REDIS_URL", ""),
		RedisWriteThrottle: getenvDur("AVALANT_REDIS_WRITE_THROTTLE", 50*time.Millisecond),
		FileDumpInterval:   getenvDur("AVALANT_FILE_DUMP_INTERVAL", 100*time.Millisecond),
		PrewarmTopN:        getenvInt("AVALANT_PREWARM_TOP_N", 20),
		IdleTimeout:        getenvDur("AVALANT_ORDERBOOK_IDLE_TIMEOUT", 60*time.Second),
		WorkerExchanges:    splitCSV(getenv("AVALANT_WORKER_EXCHANGES", "")),
		LogLevel:           getenv("LOG_LEVEL", "info"),
		WSBroadcastPort:    getenv("AVALANT_WS_BROADCAST_PORT", ""),
	}
}

func getenv(k, def string) string {
	v := os.Getenv(k)
	if v == "" {
		return def
	}
	return v
}

func getenvInt(k string, def int) int {
	v := os.Getenv(k)
	if v == "" {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return def
	}
	return n
}

func getenvDur(k string, def time.Duration) time.Duration {
	v := os.Getenv(k)
	if v == "" {
		return def
	}
	d, err := time.ParseDuration(v)
	if err != nil {
		return def
	}
	return d
}

func splitCSV(s string) []string {
	if s == "" {
		return nil
	}
	parts := strings.Split(s, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		p = strings.TrimSpace(p)
		if p != "" {
			out = append(out, p)
		}
	}
	return out
}
