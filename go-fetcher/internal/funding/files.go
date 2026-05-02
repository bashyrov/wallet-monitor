package funding

import (
	"context"
	"os"
	"path/filepath"
	"time"

	"github.com/bytedance/sonic"

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

	// Merged rows file — top-level shape: {ts, exchanges, rows[]}.
	// Python emits ts as INT seconds (epoch) and exchanges as the
	// list of enabled venues; matching exactly.
	rows := buildRows(byEx)
	exchanges := make([]string, 0, len(byEx))
	for ex := range byEx {
		exchanges = append(exchanges, ex)
	}
	merged := map[string]any{
		"ts":        time.Now().Unix(),
		"exchanges": exchanges,
		"rows":      rows,
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

// buildRows produces per-(symbol, exchange) rows matching Python's
// arbitrage_service shape — that's what the web roles + the Go arb
// compute layer both consume.
//
// Row shape (verified against prod /tmp/avalant_cache/funding.json):
//
//	{symbol, exchange, rate, price, volume_usd, next_ts (epoch sec),
//	 interval_h, apr, cross_listed}
//
// cross_listed = true when the symbol appears on >1 venue. Python's
// arb compute uses this as a coarse pre-filter on /api/screener/funding
// to drop venue-exclusive coins.
func buildRows(byEx map[string]map[string]Tick) []map[string]any {
	// Pass 1: count per-symbol presence to compute cross_listed.
	presence := make(map[string]int, 256)
	for _, bucket := range byEx {
		for sym := range bucket {
			presence[sym]++
		}
	}

	rows := make([]map[string]any, 0, 1024)
	for ex, bucket := range byEx {
		for sym, t := range bucket {
			intH := t.IntervalH
			if intH <= 0 {
				intH = 8
			}
			// APR — same formula as Python: rate * (8760 / interval_h) * 100
			rateNorm8h := t.Rate * (8.0 / intH)
			apr := rateNorm8h * (8760.0 / 8.0) * 100.0

			row := map[string]any{
				"symbol":       sym,
				"exchange":     ex,
				"rate":         t.Rate,
				"price":        t.MarkPrice,
				"volume_usd":   t.Volume24h,
				"interval_h":   intH,
				"apr":          apr,
				"cross_listed": presence[sym] > 1,
			}
			if !t.NextFunding.IsZero() {
				row["next_ts"] = t.NextFunding.Unix()
			} else {
				row["next_ts"] = 0
			}
			rows = append(rows, row)
		}
	}
	return rows
}

// No fsync — funding.json is ephemeral cache; the rename(tmp→final) is
// the consistency anchor for readers, and a crash loses at most the
// last 500ms tick which the next dump replaces.
func writeAtomic(path string, v any) error {
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, "."+filepath.Base(path)+".tmp.")
	if err != nil {
		return err
	}
	tmpPath := tmp.Name()
	data, err := sonic.ConfigStd.Marshal(v)
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
