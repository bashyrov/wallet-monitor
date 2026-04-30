package cache

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/bytedance/sonic"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
)

// Dumper periodically writes the cache to per-exchange JSON files +
// books.master.json. Atomic: write to .tmp then rename — POSIX guarantees
// the rename is atomic on the same filesystem, so readers (Python web)
// never see a partial file (bug #12).
//
// File layout matches Python:
//
//	books.<exchange>.json     — per-venue, owned by that worker
//	books.master.json         — merge of spot + paradex + extended (less hot)
//	books.json                — full merge (used by web's /ws/book)
//	funding.json              — written by funding runner, not us
//	spot_arbitrage.json       — written by arb runner, not us
type Dumper struct {
	store    *Store
	cacheDir string
	interval time.Duration
}

func NewDumper(store *Store, cacheDir string, interval time.Duration) *Dumper {
	return &Dumper{store: store, cacheDir: cacheDir, interval: interval}
}

// Run blocks until ctx is cancelled, dumping the cache every interval.
func (d *Dumper) Run(ctx context.Context) error {
	if err := os.MkdirAll(d.cacheDir, 0o755); err != nil {
		return fmt.Errorf("mkdir cache dir: %w", err)
	}
	t := time.NewTicker(d.interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-t.C:
			if err := d.dump(); err != nil {
				log.L().Warn().Err(err).Msg("file dump failed")
			}
		}
	}
}

// dump writes books.json (full merge) plus per-exchange splits.
func (d *Dumper) dump() error {
	snap := d.store.Snapshot()

	// Bucket keys by exchange.
	byExchange := make(map[string]map[string]Entry, 32)
	for k, v := range snap {
		// key = "exchange:symbol" — but spot keys can be "binance_spot:BTC"
		// which still has only one ":" — so we split on first colon only.
		idx := strings.IndexByte(k, ':')
		if idx <= 0 {
			continue
		}
		ex := k[:idx]
		sym := k[idx+1:]
		bucket, ok := byExchange[ex]
		if !ok {
			bucket = make(map[string]Entry, 32)
			byExchange[ex] = bucket
		}
		bucket[sym] = v
	}

	// Per-exchange dumps (books.<ex>.json) — Python expects this
	// format: {"<sym>": {"bids": [...], "asks": [...], "ts": ..., ...}}
	for ex, bucket := range byExchange {
		out := make(map[string]any, len(bucket))
		for sym, e := range bucket {
			out[sym] = entryToJSON(e)
		}
		path := filepath.Join(d.cacheDir, "books."+ex+".json")
		if err := writeAtomic(path, out); err != nil {
			return fmt.Errorf("dump %s: %w", ex, err)
		}
	}

	// Full merge (books.json) — flat "exchange:symbol" → entry.
	merged := make(map[string]any, len(snap))
	for k, e := range snap {
		merged[k] = entryToJSON(e)
	}
	if err := writeAtomic(filepath.Join(d.cacheDir, "books.json"), merged); err != nil {
		return err
	}

	// books.master.json — subset for spot/exotic venues so Python's
	// merger stays happy if it's still consuming this file in parallel.
	if err := d.writeMasterFile(snap); err != nil {
		return err
	}
	return nil
}

// entryToJSON renders one cache entry into the shape Python expects. This
// is the source-of-truth for the wire contract — bumping any field name
// here is a breaking change.
func entryToJSON(e Entry) map[string]any {
	return map[string]any{
		"bids":         e.Bids,
		"asks":         e.Asks,
		"ts":           e.UpdatedAt.UnixMilli(),
		"last_request": e.LastRequestAt.UnixMilli(),
		"source":       e.Source,
	}
}

// writeAtomic encodes v to path via tempfile + rename. Crash-safe: a partial
// .tmp survives but is never seen by readers, since rename is atomic on
// the same filesystem.
func writeAtomic(path string, v any) error {
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, "."+filepath.Base(path)+".tmp.")
	if err != nil {
		return err
	}
	tmpPath := tmp.Name()
	enc := sonic.ConfigStd
	data, err := enc.Marshal(v)
	if err != nil {
		tmp.Close()
		os.Remove(tmpPath)
		return err
	}
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		os.Remove(tmpPath)
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		os.Remove(tmpPath)
		return err
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmpPath)
		return err
	}
	if err := os.Rename(tmpPath, path); err != nil {
		os.Remove(tmpPath)
		return err
	}
	return nil
}
