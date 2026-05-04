// Package arb is the Go port of Python's arbitrage_service.py compute
// loop. Reads funding ticks from the funding.Store, builds cross-venue
// arb opportunities, writes arbitrage.json in the exact shape the
// Python web roles expect.
//
// What's intentionally NOT ported (yet):
//
//   - Token-registry contract validation for >100% spreads. Python uses
//     this to drop ticker-collision rows like ASTEROID-binance vs
//     ASTEROID-aster (different tokens, same ticker). For Go we apply
//     the same |price_spread|>1.0 threshold but emit anyway — the
//     Python web can still re-filter on read if needed.
//   - admin_settings hidden_symbols / disabled_exchanges hooks. The
//     filter happens at read-time on the web side.
//   - Per-symbol orderbook In/Out columns. Python dropped these too;
//     basis-only metric matches.
package arb

import (
	"context"
	"math"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"

	"github.com/bytedance/sonic"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
)

// EXCHANGE_FEES — same map as Python's EXCHANGE_FEES. Taker fees per venue.
var exchangeFees = map[string]float64{
	"binance":     0.0004,
	"bybit":       0.00055,
	"okx":         0.0005,
	"gate":        0.0005,
	"kucoin":      0.0006,
	"mexc":        0.0002,
	"bitget":      0.0006,
	"hyperliquid": 0.00035,
	"aster":       0.0005,
	"ethereal":    0.0003,
	"whitebit":    0.0006,
	"bingx":       0.0005,
	"paradex":     0.0003,
	"backpack":    0.0005,
	"htx":         0.0005,
	"kraken":      0.0005,
	"lighter":     0.0003,
}
const defaultFee = 0.0006

func feeOf(ex string) float64 {
	if v, ok := exchangeFees[ex]; ok {
		return v
	}
	return defaultFee
}

const (
	// Hysteresis windows — same as Python.
	oppMinLifetime = 3 * time.Second
	oppPurgeAfter  = 30 * time.Second

	// Spread sanity-cap: rows with |price_spread|>100% are usually
	// ticker-collisions. Python's threshold; we keep it.
	highSpreadThreshold = 1.0

	// File-cache cap — top-N kept in arbitrage.json. Bumped from 500 to
	// 1000: the screener's "tracked set" is whatever ends up in this
	// file, and the user-touch path auto-subscribes orderbooks for
	// every (sym, ex) pair we surface. Top-1000 by basis covers more
	// of the actionable arb space without saturating the WS layer.
	arbFileTopN = 1000

	// Default volume floor — 24h USD volume. Drops delisted/halted
	// contracts that the venue still emits funding rows for, plus
	// dead-microcap pairs which would dominate the screener with
	// stale data. User policy: 20k USD/24h is the floor.
	minVolumeUSD = 20_000
)

// Compute is the periodic arb-compute service. Reads funding.Store on a
// ticker, builds opportunities, dumps arbitrage.json.
type Compute struct {
	store    *funding.Store
	books    *cache.Store // optional — for baking in/out top-of-book into each opp
	cacheDir string
	interval time.Duration

	mu          sync.Mutex
	firstSeen   map[oppKey]time.Time
	lastSeen    map[oppKey]time.Time
}

type oppKey struct {
	symbol      string
	long, short string
}

func NewCompute(store *funding.Store, books *cache.Store, cacheDir string, interval time.Duration) *Compute {
	return &Compute{
		store:     store,
		books:     books,
		cacheDir:  cacheDir,
		interval:  interval,
		firstSeen: make(map[oppKey]time.Time, 4096),
		lastSeen:  make(map[oppKey]time.Time, 4096),
	}
}

func (c *Compute) Run(ctx context.Context) error {
	t := time.NewTicker(c.interval)
	defer t.Stop()
	// Wait briefly so the funding store is non-empty before first tick.
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-time.After(2 * time.Second):
	}
	c.tick()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-t.C:
			c.tick()
		}
	}
}

func (c *Compute) tick() {
	now := time.Now()
	byEx := c.store.SnapshotByExchange()

	// Bucket by symbol → list of (exchange, tick).
	type entry struct {
		ex string
		t  funding.Tick
	}
	bySym := make(map[string][]entry, 1024)
	exchanges := make(map[string]struct{}, len(byEx))
	for ex, bucket := range byEx {
		exchanges[ex] = struct{}{}
		for sym, t := range bucket {
			if t.Rate == 0 && t.MarkPrice == 0 {
				continue // empty entry, skip
			}
			if t.IntervalH <= 0 {
				continue
			}
			// Filter delisted/halted contracts by venue exchangeInfo.
			// Aster ships SETTLING (DAM/MATIC), Binance ships
			// SETTLING/BREAK (NTRN). HL filters via isDelisted in
			// its own adapter.
			if !IsListed(ex, sym) {
				continue
			}
			// Default volume filter — drop microcap noise. 20k USD/24h
			// is the user-requested floor: real trading pairs always
			// clear this; delisted/dead pairs typically don't.
			if t.Volume24h > 0 && t.Volume24h < minVolumeUSD {
				continue
			}
			bySym[sym] = append(bySym[sym], entry{ex: ex, t: t})
		}
	}

	type pe struct {
		ex       string
		ivl      float64
		rateNorm float64
		fee      float64
		mark     float64
		volUSD   float64
		nextTs   int64
	}

	opps := make([]map[string]any, 0, 1024)

	c.mu.Lock()
	for sym, entries := range bySym {
		if len(entries) < 2 {
			continue
		}
		preEntries := make([]pe, 0, len(entries))
		for _, e := range entries {
			rateNorm := e.t.Rate * (8.0 / e.t.IntervalH)
			preEntries = append(preEntries, pe{
				ex:       e.ex,
				ivl:      e.t.IntervalH,
				rateNorm: rateNorm,
				fee:      feeOf(e.ex),
				mark:     e.t.MarkPrice,
				volUSD:   e.t.Volume24h,
				nextTs:   nextTsOf(e.t.NextFunding),
			})
		}

		for i, longPE := range preEntries {
			for j, shortPE := range preEntries {
				if i == j {
					continue
				}

				gross := shortPE.rateNorm - longPE.rateNorm
				totalFees := 2.0 * (longPE.fee + shortPE.fee)

				if longPE.mark <= 0 || shortPE.mark <= 0 {
					continue
				}
				// Ticker-collision guard: when the two venues' marks differ
				// by more than 50% (ratio test, not bps), the ticker almost
				// certainly identifies different tokens — e.g. EDGE on Gate
				// trades at $0.10 vs $1.20 on Aster/Binance/OKX/Bybit (12×
				// gap). The previous |price_spread| > 100% threshold let
				// 12× collisions pass because they're below 100% in
				// absolute terms only when computed off the larger leg.
				// max/min ratio is symmetric and unambiguous.
				hi, lo := longPE.mark, shortPE.mark
				if hi < lo {
					hi, lo = lo, hi
				}
				if lo <= 0 || hi/lo > 1.5 {
					continue
				}
				priceSpread := (shortPE.mark - longPE.mark) / longPE.mark
				// Net/8h uses live entry basis (in_pct from top-of-book) when
				// available — that's what an actual entry-now would capture.
				// Falls back to mark-based price_spread until the orderbook
				// tick lands. APR is funding-only (no spread/in component) so
				// it represents sustainable annual return, not a one-shot
				// entry pickup.
				inPctPtr, _ := computeInOutPair(c.books, longPE.ex, shortPE.ex, sym)
				entryBasis := priceSpread
				if inPctPtr != nil {
					entryBasis = *inPctPtr / 100.0 // computeInOutPair returns %
				}
				net := gross + entryBasis - totalFees
				netFundingOnly := gross - totalFees

				// |price_spread|>100% — extra belt for any case the ratio
				// test let through (shouldn't happen if ratio < 1.5).
				if math.Abs(priceSpread) > highSpreadThreshold {
					continue
				}

				key := oppKey{symbol: sym, long: longPE.ex, short: shortPE.ex}
				first, ok := c.firstSeen[key]
				if !ok {
					c.firstSeen[key] = now
					c.lastSeen[key] = now
					continue
				}
				c.lastSeen[key] = now
				if now.Sub(first) < oppMinLifetime {
					continue
				}

				// Bake live entry/exit basis from top-of-book directly
				// into the opp. Frontend reads in_pct/out_pct from this
				// payload — no separate /api/screener/in-out call. Avoids
				// the round-trip + URL-length + disk-thrash issues we
				// saw when /in-out polled every 3 s with 256-key batches.
				inPct := inPctPtr
				_, outPct := c.computeInOut(longPE.ex, shortPE.ex, sym)
				opps = append(opps, map[string]any{
					"symbol":            sym,
					"long_exchange":     longPE.ex,
					"short_exchange":    shortPE.ex,
					"long_rate":         round6(longPE.rateNorm * 100),
					"short_rate":        round6(shortPE.rateNorm * 100),
					"long_price":        longPE.mark,
					"short_price":       shortPE.mark,
					"long_volume":       longPE.volUSD,
					"short_volume":      shortPE.volUSD,
					"gross_funding":     round6(gross * 100),
					"price_spread":      round4(priceSpread * 100),
					"fee_long":          round4(longPE.fee * 100),
					"fee_short":         round4(shortPE.fee * 100),
					"total_fees":        round4(totalFees * 100),
					"net_profit":        round6(net * 100),
					"gross_apr":         round4(gross * (8760.0 / 8.0) * 100),
					"net_apr":           round4(netFundingOnly * (8760.0 / 8.0) * 100),
					"valid_price":       longPE.mark <= shortPE.mark,
					"next_ts_long":      longPE.nextTs,
					"next_ts_short":     shortPE.nextTs,
					"long_interval_h":   longPE.ivl,
					"short_interval_h":  shortPE.ivl,
					"in_pct":            inPct,
					"out_pct":           outPct,
				})
			}
		}
	}

	// Purge hysteresis entries that haven't been observed in a while.
	cutoff := now.Add(-oppPurgeAfter)
	for k, ts := range c.lastSeen {
		if ts.Before(cutoff) {
			delete(c.firstSeen, k)
			delete(c.lastSeen, k)
		}
	}
	c.mu.Unlock()

	// Sort by |basis| (price_spread) descending — that's the cap-key.
	// The file becomes the screener's "tracked set": pairs end up here
	// when their basis is wide, and the symbol manager auto-subscribes
	// their books on user-touch. Within the top-N the frontend re-sorts
	// by live in/out for the user-visible ordering.
	sort.Slice(opps, func(i, j int) bool {
		ai, _ := opps[i]["price_spread"].(float64)
		aj, _ := opps[j]["price_spread"].(float64)
		if ai < 0 {
			ai = -ai
		}
		if aj < 0 {
			aj = -aj
		}
		return ai > aj
	})

	// Cap to top-N for the file write.
	written := opps
	if len(written) > arbFileTopN {
		written = written[:arbFileTopN]
	}

	exList := make([]string, 0, len(exchanges))
	for ex := range exchanges {
		exList = append(exList, ex)
	}
	feesPct := make(map[string]float64, len(exchangeFees))
	for ex, f := range exchangeFees {
		feesPct[ex] = round4(f * 100)
	}

	out := map[string]any{
		"ts":            now.Unix(),
		"exchanges":     exList,
		"fees":          feesPct,
		"opportunities": written,
	}
	if len(written) < len(opps) {
		out["truncated_to"] = arbFileTopN
	}
	if err := writeAtomic(filepath.Join(c.cacheDir, "arbitrage.json"), out); err != nil {
		log.L().Warn().Err(err).Msg("arb write failed")
	}
}

// computeInOut is a method shim over computeInOutPair for the futures
// Compute struct (legacy receiver shape).
func (c *Compute) computeInOut(longEx, shortEx, sym string) (*float64, *float64) {
	return computeInOutPair(c.books, longEx, shortEx, sym)
}

// computeInOutPair returns top-of-book entry/exit basis as percentages.
// On a clean read it caches (in, out) keyed by the pair so a transient
// book hiccup (WS resub, REST gap, prune→resub) returns the LAST GOOD
// value for up to inOutStickyTTL instead of nil. Without this the
// screener row was disappearing for a frame whenever a book briefly
// emptied, which read as the metric "blinking to 0" on the user's side.
//
//	in_pct  = (bestBidShort - bestAskLong)  / bestAskLong  * 100
//	out_pct = (bestBidLong  - bestAskShort) / bestAskShort * 100
//
// Used by all three arb compute paths (futures / spot / dex).
func computeInOutPair(books *cache.Store, longEx, shortEx, sym string) (*float64, *float64) {
	if books == nil {
		return nil, nil
	}
	longE, lok := books.Get(longEx, sym)
	shortE, sok := books.Get(shortEx, sym)
	bothPresent := lok && sok &&
		len(longE.Asks) > 0 && len(longE.Bids) > 0 &&
		len(shortE.Asks) > 0 && len(shortE.Bids) > 0
	if bothPresent {
		bestAskLong := longE.Asks[0][0]
		bestBidLong := longE.Bids[0][0]
		bestAskShort := shortE.Asks[0][0]
		bestBidShort := shortE.Bids[0][0]
		if bestAskLong > 0 && bestBidLong > 0 && bestAskShort > 0 && bestBidShort > 0 {
			in := round4((bestBidShort - bestAskLong) / bestAskLong * 100)
			out := round4((bestBidLong - bestAskShort) / bestAskShort * 100)
			inOutCache.put(longEx, shortEx, sym, in, out)
			return &in, &out
		}
	}
	// Stale fall-through — keep showing last good value within TTL so
	// the row doesn't blink during a brief book gap. After TTL we
	// finally return nil and the row drops out.
	if in, out, ok := inOutCache.get(longEx, shortEx, sym); ok {
		return &in, &out
	}
	return nil, nil
}

const inOutStickyTTL = 8 * time.Second

type inOutEntry struct {
	in, out float64
	at      time.Time
}

type inOutCacheT struct {
	mu sync.Mutex
	m  map[string]inOutEntry
}

func (c *inOutCacheT) put(le, se, sym string, in, out float64) {
	c.mu.Lock()
	if c.m == nil {
		c.m = map[string]inOutEntry{}
	}
	c.m[le+"|"+se+"|"+sym] = inOutEntry{in: in, out: out, at: time.Now()}
	c.mu.Unlock()
}

func (c *inOutCacheT) get(le, se, sym string) (float64, float64, bool) {
	c.mu.Lock()
	e, ok := c.m[le+"|"+se+"|"+sym]
	c.mu.Unlock()
	if !ok {
		return 0, 0, false
	}
	if time.Since(e.at) > inOutStickyTTL {
		return 0, 0, false
	}
	return e.in, e.out, true
}

var inOutCache inOutCacheT

// computeInOutDex — DEX/short variant. The "long" side has no orderbook
// (it's a DEX with a single mid price baked into the opp at compute
// time); only the perp short side has a book to walk. Computes:
//
//	in_pct  = (bestBidShort - dexPrice)   / dexPrice    * 100
//	out_pct = (dexPrice - bestAskShort)   / bestAskShort * 100
func computeInOutDex(books *cache.Store, shortEx, sym string, dexPrice float64) (*float64, *float64) {
	if books == nil || dexPrice <= 0 {
		return nil, nil
	}
	shortE, ok := books.Get(shortEx, sym)
	bookOK := ok && len(shortE.Asks) > 0 && len(shortE.Bids) > 0
	if bookOK {
		bestAskShort := shortE.Asks[0][0]
		bestBidShort := shortE.Bids[0][0]
		if bestAskShort > 0 && bestBidShort > 0 {
			in := round4((bestBidShort - dexPrice) / dexPrice * 100)
			out := round4((dexPrice - bestAskShort) / bestAskShort * 100)
			// Sticky-cache the same way as the futures/spot path so a
			// brief WS gap on the perp leg doesn't flap the row.
			inOutCache.put("dex", shortEx, sym, in, out)
			return &in, &out
		}
	}
	if in, out, ok := inOutCache.get("dex", shortEx, sym); ok {
		return &in, &out
	}
	return nil, nil
}

func nextTsOf(t time.Time) int64 {
	if t.IsZero() {
		return 0
	}
	return t.Unix()
}

func round4(x float64) float64 { return math.Round(x*1e4) / 1e4 }
func round6(x float64) float64 { return math.Round(x*1e6) / 1e6 }

// writeAtomic — own copy because importing cache would create a cycle
// (cache → ws, but funding/arb shouldn't depend on cache). Tiny.
//
// No fsync — these are ephemeral cache files (arbitrage.json, etc.); a
// crash loses at most the latest tick's dump, which the next cycle
// rewrites. Skipping fsync removes the per-write disk-flush wait that
// was the dominant cost on the 700ms compute cadence.
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
	return os.Rename(tmpPath, path)
}
