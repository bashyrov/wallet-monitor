package funding

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
)

// Dumper writes the funding store to disk on a tick. One file per venue
// at funding.<exchange>.json (matches Python's per-venue layout) plus a
// merged funding.json with the cross-venue rows shape consumers read.
//
// The merged structure matches Python's funding.json:
//
//	{
//	  "rows": [
//	    {"symbol":"BTC", "rates":{"binance":0.0001,"okx":0.00012,...},
//	                     "marks":{"binance":76000.5,...},
//	                     "vols":{"binance":12345.6,...},
//	                     "max_spread":0.00018,
//	                     "next_ts":<epoch_ms>},
//	    ...
//	  ],
//	  "ts": <epoch_ms>
//	}
type Dumper struct {
	store    *Store
	cacheDir string
	interval time.Duration
	// Optional cross-pollination source. When set, every dump pass
	// fills mark_price on funding ticks where the REST endpoint
	// doesn't carry it (HTX is the canonical case) by looking up the
	// midprice from the orderbook cache. obSource takes (exchange,
	// symbol) and returns (bestBid, bestAsk, ok).
	obSource func(exchange, symbol string) (bestBid, bestAsk float64, ok bool)
}

func NewDumper(store *Store, cacheDir string, interval time.Duration) *Dumper {
	return &Dumper{store: store, cacheDir: cacheDir, interval: interval}
}

// SetOrderbookSource enables HTX-class mark-price fill from the live
// orderbook cache. Caller passes a bound closure on the *cache.Store —
// avoids importing cache here and any potential cycle.
func (d *Dumper) SetOrderbookSource(fn func(exchange, symbol string) (bestBid, bestAsk float64, ok bool)) {
	d.obSource = fn
}

func (d *Dumper) Run(ctx context.Context) error {
	if err := os.MkdirAll(d.cacheDir, 0o755); err != nil {
		return err
	}
	t := time.NewTicker(d.interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-t.C:
			if err := d.dump(); err != nil {
				log.L().Warn().Err(err).Msg("funding dump failed")
			}
		}
	}
}

func (d *Dumper) dump() error {
	byEx := d.store.SnapshotByExchange()

	// Cross-pollinate: HTX-class venues that report rate-only via REST
	// inherit mark price from the orderbook midprice.
	if d.obSource != nil {
		for venue, bucket := range byEx {
			for sym, t := range bucket {
				if t.MarkPrice != 0 {
					continue
				}
				bid, ask, ok := d.obSource(venue, sym)
				if !ok || bid <= 0 || ask <= 0 {
					continue
				}
				t.MarkPrice = (bid + ask) / 2
				bucket[sym] = t
			}
		}
	}

	// Per-venue files
	for ex, bucket := range byEx {
		out := make(map[string]any, len(bucket))
		for sym, t := range bucket {
			out[sym] = tickToJSON(t)
		}
		path := filepath.Join(d.cacheDir, "funding."+ex+".json")
		if err := writeAtomic(path, out); err != nil {
			return err
		}
	}

	// Merged rows file
	rows := buildRows(byEx)
	merged := map[string]any{
		"rows": rows,
		"ts":   time.Now().UnixMilli(),
	}
	if err := writeAtomic(filepath.Join(d.cacheDir, "funding.json"), merged); err != nil {
		return err
	}
	return nil
}

func tickToJSON(t Tick) map[string]any {
	out := map[string]any{
		"rate":         t.Rate,
		"mark_price":   t.MarkPrice,
		"index_price":  t.IndexPrice,
		"volume_24h":   t.Volume24h,
		"open_int_usd": t.OpenIntUSD,
		"interval_h":   t.IntervalH,
		"updated_at":   t.UpdatedAt.UnixMilli(),
	}
	if !t.NextFunding.IsZero() {
		out["next_funding"] = t.NextFunding.UnixMilli()
	}
	return out
}

// buildRows pivots {ex: {sym: tick}} → [{symbol, rates: {ex: rate}, ...}]
// and computes max_spread (per-symbol max abs(rate_i - rate_j) across
// venues). Python's screener consumes this shape directly.
func buildRows(byEx map[string]map[string]Tick) []map[string]any {
	// flatten — collect all symbols seen
	syms := make(map[string]struct{}, 256)
	for _, bucket := range byEx {
		for sym := range bucket {
			syms[sym] = struct{}{}
		}
	}

	rows := make([]map[string]any, 0, len(syms))
	for sym := range syms {
		rates := make(map[string]float64, 16)
		marks := make(map[string]float64, 16)
		vols := make(map[string]float64, 16)
		nexts := make(map[string]int64, 16)
		intervals := make(map[string]float64, 16)

		for ex, bucket := range byEx {
			t, ok := bucket[sym]
			if !ok {
				continue
			}
			rates[ex] = t.Rate
			if t.MarkPrice != 0 {
				marks[ex] = t.MarkPrice
			}
			if t.Volume24h != 0 {
				vols[ex] = t.Volume24h
			}
			if !t.NextFunding.IsZero() {
				nexts[ex] = t.NextFunding.UnixMilli()
			}
			if t.IntervalH != 0 {
				intervals[ex] = t.IntervalH
			}
		}

		// max_spread = max - min across the available venues
		var maxSpread float64
		var min, max float64
		first := true
		for _, r := range rates {
			if first {
				min, max = r, r
				first = false
				continue
			}
			if r < min {
				min = r
			}
			if r > max {
				max = r
			}
		}
		if !first {
			maxSpread = max - min
		}

		rows = append(rows, map[string]any{
			"symbol":     sym,
			"rates":      rates,
			"marks":      marks,
			"vols":       vols,
			"nexts":      nexts,
			"intervals":  intervals,
			"max_spread": maxSpread,
		})
	}

	return rows
}

func writeAtomic(path string, v any) error {
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, "."+filepath.Base(path)+".tmp.")
	if err != nil {
		return err
	}
	tmpPath := tmp.Name()
	enc := json.NewEncoder(tmp)
	if err := enc.Encode(v); err != nil {
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
