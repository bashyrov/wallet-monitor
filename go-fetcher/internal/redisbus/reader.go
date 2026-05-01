// reader.go — orderbook batch reads, mirror of Python's
// orderbook_redis.read_books_batch. Used by the /ws/book broadcaster
// to fan out per-pair updates without per-key round-trips.
package redisbus

import (
	"context"
	"time"

	"github.com/bytedance/sonic"
	"github.com/redis/go-redis/v9"
)

// Reader pulls `ob:<exchange>:<symbol>` keys from Redis. One MGET per
// call regardless of pair count — same shape as Python's read_books_batch.
type Reader struct {
	client *redis.Client
}

// BookEntry — decoded `{ts, data:{bids, asks}}` blob.
type BookEntry struct {
	TS   float64                  `json:"ts"`
	Data map[string][][]float64   `json:"data"`
}

// NewReader connects to Redis. Returns nil if redisURL is empty so the
// caller can run in file-only mode.
func NewReader(redisURL string) (*Reader, error) {
	if redisURL == "" {
		return nil, nil
	}
	opts, err := redis.ParseURL(redisURL)
	if err != nil {
		return nil, err
	}
	return &Reader{client: redis.NewClient(opts)}, nil
}

// ReadBooks performs one MGET across every requested pair. Returns a
// map of `<ex>:<sym>` → BookEntry. Missing or malformed entries are
// silently omitted so the caller doesn't have to handle nils.
func (r *Reader) ReadBooks(ctx context.Context, pairs []string) map[string]BookEntry {
	if r == nil || len(pairs) == 0 {
		return nil
	}
	keys := make([]string, len(pairs))
	for i, p := range pairs {
		keys[i] = "ob:" + p
	}
	mgetCtx, cancel := context.WithTimeout(ctx, 1*time.Second)
	defer cancel()
	raws, err := r.client.MGet(mgetCtx, keys...).Result()
	if err != nil {
		return nil
	}
	out := make(map[string]BookEntry, len(pairs))
	for i, raw := range raws {
		if raw == nil {
			continue
		}
		s, ok := raw.(string)
		if !ok {
			continue
		}
		var entry BookEntry
		if err := sonic.UnmarshalString(s, &entry); err != nil {
			continue
		}
		out[pairs[i]] = entry
	}
	return out
}

func (r *Reader) Close() error {
	if r == nil {
		return nil
	}
	return r.client.Close()
}
