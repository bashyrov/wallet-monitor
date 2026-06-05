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

	// Per-key Redis-write throttle. Default 10ms (100 Hz) — lower than
	// the old 50ms default to allow hot pairs to reach flushLoop's 20 Hz
	// ceiling. Override via AVALANT_REDIS_WRITE_THROTTLE (e.g. "20ms").
	// Setting 0 disables throttle entirely (bypass).
	RedisWriteThrottle time.Duration

	// Toggle for the Redis orderbook mirror (`ob:<ex>:<sym>` SETEX
	// on every cache update). Default true preserves current prod
	// behaviour. Set false once Python REST callers have been
	// verified to fall through cleanly to the file cache — the
	// Writer accounts for an estimated 2-3 cores on go-fetcher under
	// load (every Store call spawns a goroutine for SETEX). Path to
	// removal: flip to false on prod → soak → change default →
	// delete code.
	RedisBookWriteEnabled bool

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
		CacheDir:              getenv("AVALANT_FETCHER_CACHE_DIR", "/tmp/avalant_cache"),
		RedisURL:              getenv("REDIS_URL", ""),
		RedisWriteThrottle:    getenvDur("AVALANT_REDIS_WRITE_THROTTLE", 10*time.Millisecond),
		RedisBookWriteEnabled: getenvBool("AVALANT_REDIS_BOOK_WRITE", true),
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

func getenvBool(k string, def bool) bool {
	v := strings.TrimSpace(strings.ToLower(os.Getenv(k)))
	switch v {
	case "":
		return def
	case "1", "true", "yes", "on":
		return true
	case "0", "false", "no", "off":
		return false
	default:
		return def
	}
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
