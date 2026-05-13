package arb

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

// seedBook places a single-level (bid, ask) on (ex, sym) in the
// orderbook cache for in/out-pct tests.
func seedBook(s *cache.Store, ex, sym string, bestBid, bestAsk float64) {
	s.Store(ex, sym, ws.Snapshot{
		Symbol: sym,
		Bids:   []ws.Level{{bestBid, 1.0}},
		Asks:   []ws.Level{{bestAsk, 1.0}},
	}, "ws")
}

// seedFunding adds a Tick for (exchange, symbol) with the given rate/mark
// (both venues need IntervalH=8 by default to match Binance/Bybit/OKX).
func seedFunding(s *funding.Store, ex, sym string, rate, mark, vol float64) {
	s.Apply(ex, funding.Tick{
		Symbol: sym, Rate: rate, MarkPrice: mark,
		Volume24h: vol, IntervalH: 8,
	})
}

// preBypass marks the (sym, long, short) pair as seen long enough ago
// that the oppMinLifetime hysteresis (1 s) is satisfied on the next tick.
func preBypass(c *Compute, sym, long, short string) {
	k := oppKey{symbol: sym, long: long, short: short}
	old := time.Now().Add(-10 * time.Second)
	c.mu.Lock()
	c.firstSeen[k] = old
	c.lastSeen[k] = old
	c.mu.Unlock()
}

func readArbFile(t *testing.T, dir string) map[string]any {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(dir, "arbitrage.json"))
	if err != nil {
		t.Fatalf("read arbitrage.json: %v", err)
	}
	var doc map[string]any
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("decode: %v", err)
	}
	return doc
}

func TestCompute_Tick_WritesArbitrageJSON(t *testing.T) {
	dir := t.TempDir()
	fs := funding.NewStore()
	bs := cache.New()
	// Two venues quote BTC with a 0.0001 rate gap → arb opp.
	seedFunding(fs, "binance", "BTC", 0.0001, 60000, 1e9)
	seedFunding(fs, "bybit", "BTC", 0.0002, 60050, 1e9)

	c := NewCompute(fs, bs, dir, 100*time.Millisecond)
	// First tick seeds firstSeen/lastSeen — nothing emitted (hysteresis).
	c.tick()
	doc := readArbFile(t, dir)
	opps := doc["opportunities"].([]any)
	if len(opps) != 0 {
		t.Errorf("first tick should be empty (hysteresis), got %d", len(opps))
	}

	// Bypass hysteresis for both directions
	preBypass(c, "BTC", "binance", "bybit")
	preBypass(c, "BTC", "bybit", "binance")
	c.tick()

	doc = readArbFile(t, dir)
	opps = doc["opportunities"].([]any)
	if len(opps) < 1 {
		t.Fatalf("after bypass: expected ≥1 opp got %d", len(opps))
	}
	first := opps[0].(map[string]any)
	if first["symbol"] != "BTC" {
		t.Errorf("symbol: %v", first["symbol"])
	}
	// One of (binance,bybit) is long, the other short.
	le := first["long_exchange"].(string)
	se := first["short_exchange"].(string)
	if !((le == "binance" && se == "bybit") || (le == "bybit" && se == "binance")) {
		t.Errorf("legs: %s / %s", le, se)
	}
}

func TestCompute_Tick_FiltersRatioCollision(t *testing.T) {
	// hi/lo > 1.5 ratio guard — different tokens that share a ticker.
	dir := t.TempDir()
	fs := funding.NewStore()
	bs := cache.New()
	seedFunding(fs, "binance", "EDGE", 0.0001, 1.20, 1e9)
	seedFunding(fs, "gate", "EDGE", 0.0002, 0.10, 1e9) // 12× gap
	c := NewCompute(fs, bs, dir, 100*time.Millisecond)
	preBypass(c, "EDGE", "binance", "gate")
	preBypass(c, "EDGE", "gate", "binance")
	c.tick()
	doc := readArbFile(t, dir)
	if len(doc["opportunities"].([]any)) != 0 {
		t.Errorf("ratio-collision pair should be filtered out, got %v", doc["opportunities"])
	}
}

func TestCompute_Tick_FiltersMissingIntervalH(t *testing.T) {
	dir := t.TempDir()
	fs := funding.NewStore()
	bs := cache.New()
	// IntervalH=0 means "no data" — skipped per tick() guard.
	fs.Apply("binance", funding.Tick{Symbol: "BTC", Rate: 0.0001, MarkPrice: 60000, Volume24h: 1e9, IntervalH: 0})
	fs.Apply("bybit", funding.Tick{Symbol: "BTC", Rate: 0.0002, MarkPrice: 60050, Volume24h: 1e9, IntervalH: 0})
	c := NewCompute(fs, bs, dir, 100*time.Millisecond)
	preBypass(c, "BTC", "binance", "bybit")
	preBypass(c, "BTC", "bybit", "binance")
	c.tick()
	doc := readArbFile(t, dir)
	if len(doc["opportunities"].([]any)) != 0 {
		t.Errorf("missing IntervalH should filter, got %v", doc["opportunities"])
	}
}

func TestCompute_Tick_SkipsSingleVenueSymbols(t *testing.T) {
	dir := t.TempDir()
	fs := funding.NewStore()
	bs := cache.New()
	// Only one venue has BTC — len(entries) < 2 → no arb.
	seedFunding(fs, "binance", "BTC", 0.0001, 60000, 1e9)
	c := NewCompute(fs, bs, dir, 100*time.Millisecond)
	c.tick()
	c.tick() // ensure hysteresis not the cause
	doc := readArbFile(t, dir)
	if len(doc["opportunities"].([]any)) != 0 {
		t.Errorf("single-venue symbol should have no arb, got %v", doc["opportunities"])
	}
}

func TestCompute_Tick_OutputIncludesFeesAndExchanges(t *testing.T) {
	dir := t.TempDir()
	fs := funding.NewStore()
	bs := cache.New()
	seedFunding(fs, "binance", "BTC", 0.0001, 60000, 1e9)
	seedFunding(fs, "bybit", "BTC", 0.0002, 60050, 1e9)
	c := NewCompute(fs, bs, dir, 100*time.Millisecond)
	c.tick()
	doc := readArbFile(t, dir)
	if _, ok := doc["fees"]; !ok {
		t.Errorf("output should include fees map")
	}
	if _, ok := doc["exchanges"]; !ok {
		t.Errorf("output should include exchanges list")
	}
	if _, ok := doc["ts"]; !ok {
		t.Errorf("output should include ts")
	}
}

func TestCompute_Tick_HysteresisPurge(t *testing.T) {
	dir := t.TempDir()
	fs := funding.NewStore()
	bs := cache.New()
	c := NewCompute(fs, bs, dir, 100*time.Millisecond)
	// Insert ancient last-seen — should be purged at next tick.
	c.mu.Lock()
	ancient := time.Now().Add(-10 * time.Minute)
	c.firstSeen[oppKey{symbol: "OLD", long: "x", short: "y"}] = ancient
	c.lastSeen[oppKey{symbol: "OLD", long: "x", short: "y"}] = ancient
	c.mu.Unlock()
	c.tick()
	c.mu.Lock()
	_, stillThere := c.firstSeen[oppKey{symbol: "OLD", long: "x", short: "y"}]
	c.mu.Unlock()
	if stillThere {
		t.Errorf("ancient entry should be purged after oppPurgeAfter (90s)")
	}
}

func TestComputeInOut_DelegatesToPublic(t *testing.T) {
	dir := t.TempDir()
	fs := funding.NewStore()
	bs := cache.New()
	seedBook(bs, "binance", "BTC", 99, 100)
	seedBook(bs, "bybit", "BTC", 100.4, 100.5)
	c := NewCompute(fs, bs, dir, 100*time.Millisecond)
	in, out := c.computeInOut("binance", "bybit", "BTC")
	if in == nil || out == nil {
		t.Errorf("computeInOut should delegate to ComputeInOutPair, got nil/nil")
	}
}
