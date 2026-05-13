package arb

import (
	"math"
	"testing"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

func seedBook(s *cache.Store, ex, sym string, bestBid, bestAsk float64) {
	s.Store(ex, sym, ws.Snapshot{
		Symbol: sym,
		Bids:   []ws.Level{{bestBid, 1.0}},
		Asks:   []ws.Level{{bestAsk, 1.0}},
	}, "ws")
}

func TestFeeOf_KnownVenue(t *testing.T) {
	cases := map[string]float64{
		"binance":     0.0004,
		"bybit":       0.00055,
		"hyperliquid": 0.00035,
		"paradex":     0.0003,
	}
	for ex, want := range cases {
		got := feeOf(ex)
		if got != want {
			t.Errorf("feeOf(%q): want %v got %v", ex, want, got)
		}
	}
}

func TestFeeOf_UnknownReturnsDefault(t *testing.T) {
	got := feeOf("nonexistent_venue")
	if got != defaultFee {
		t.Errorf("unknown fee: want %v got %v", defaultFee, got)
	}
}

func TestRound4_FourPlaces(t *testing.T) {
	// Note: 0.00015 cannot be represented exactly in float64 — it's
	// 0.0001499999... so round4 takes it to 0.0001 (not 0.0002).
	// Half-rounding tests on binary floats are unreliable; we use values
	// that can be expressed exactly to verify rounding behavior.
	cases := []struct {
		in, want float64
	}{
		{0.000123, 0.0001}, // truncated to 4 places
		{0.000156, 0.0002}, // rounds up (binary representation is fine here)
		{0.0001, 0.0001},   // exact
		{-0.000123, -0.0001},
		{0, 0},
	}
	for _, c := range cases {
		got := round4(c.in)
		if math.Abs(got-c.want) > 1e-9 {
			t.Errorf("round4(%v): want %v got %v", c.in, c.want, got)
		}
	}
}

func TestRound6_SixPlaces(t *testing.T) {
	got := round6(0.0000012345)
	if math.Abs(got-0.000001) > 1e-12 {
		t.Errorf("round6: %v", got)
	}
}

func TestComputeInOutPair_BothBooksPresent(t *testing.T) {
	s := cache.New()
	// Long: binance, ask=100, bid=99
	// Short: bybit, ask=100.5, bid=100.4
	seedBook(s, "binance", "BTC", 99, 100)
	seedBook(s, "bybit", "BTC", 100.4, 100.5)

	in, out := ComputeInOutPair(s, "binance", "bybit", "BTC")
	if in == nil || out == nil {
		t.Fatal("nil result")
	}
	// in_pct = (bidShort - askLong) / askLong * 100 = (100.4 - 100) / 100 * 100 = 0.4
	if math.Abs(*in-0.4) > 0.01 {
		t.Errorf("in_pct: want ~0.4 got %v", *in)
	}
	// out_pct = (bidLong - askShort) / askShort * 100 = (99 - 100.5) / 100.5 * 100 ≈ -1.49
	if math.Abs(*out-(-1.4925)) > 0.01 {
		t.Errorf("out_pct: want ~-1.49 got %v", *out)
	}
}

func TestComputeInOutPair_MissingBooksReturnsNil(t *testing.T) {
	s := cache.New()
	seedBook(s, "binance", "BTC", 99, 100)
	// short side missing
	// Use a fresh cache for this test to avoid sticky cache leaking from prior tests
	// (inOutCache is package-global). Use a unique key combo.
	in, out := ComputeInOutPair(s, "binance_test_miss_long", "bybit_test_miss_short", "BTCMISSING")
	if in != nil || out != nil {
		t.Errorf("missing book + no cache: want nil/nil, got %v/%v", in, out)
	}
}

func TestComputeInOutPair_NilStoreReturnsNil(t *testing.T) {
	in, out := ComputeInOutPair(nil, "binance", "bybit", "BTC")
	if in != nil || out != nil {
		t.Errorf("nil store: want nil/nil, got %v/%v", in, out)
	}
}

func TestComputeInOutDex_HappyPath(t *testing.T) {
	s := cache.New()
	// Perp short on binance: ask=100.5, bid=100.4
	seedBook(s, "binance", "BTC", 100.4, 100.5)
	// DEX price = 99.0
	in, out := ComputeInOutDex(s, "binance", "BTC", 99.0)
	if in == nil || out == nil {
		t.Fatal("nil result")
	}
	// in_pct = (bidShort - dexPrice) / dexPrice * 100 = (100.4-99)/99 * 100 ≈ 1.414
	if math.Abs(*in-1.4141) > 0.01 {
		t.Errorf("dex in_pct: want ~1.4141 got %v", *in)
	}
	// out_pct = (dexPrice - askShort) / askShort * 100 = (99-100.5)/100.5 * 100 ≈ -1.49
	if math.Abs(*out-(-1.4925)) > 0.01 {
		t.Errorf("dex out_pct: want ~-1.49 got %v", *out)
	}
}

func TestComputeInOutDex_ZeroDexPriceReturnsNil(t *testing.T) {
	s := cache.New()
	seedBook(s, "binance", "BTC", 100, 101)
	in, out := ComputeInOutDex(s, "binance", "BTC", 0)
	if in != nil || out != nil {
		t.Errorf("zero dex price: want nil/nil, got %v/%v", in, out)
	}
}

func TestComputeInOutDex_NilStoreReturnsNil(t *testing.T) {
	in, out := ComputeInOutDex(nil, "binance", "BTC", 100)
	if in != nil || out != nil {
		t.Errorf("nil store: want nil/nil, got %v/%v", in, out)
	}
}

func TestInOutCacheT_StickyTTLBehavior(t *testing.T) {
	c := &inOutCacheT{m: map[string]inOutEntry{}}
	c.put("ex1", "ex2", "BTC", 0.5, -0.5)
	in, out, ok := c.get("ex1", "ex2", "BTC")
	if !ok {
		t.Fatal("fresh entry missing")
	}
	if in != 0.5 || out != -0.5 {
		t.Errorf("values: %v %v", in, out)
	}
	// Backdate past TTL
	c.mu.Lock()
	e := c.m["ex1|ex2|BTC"]
	e.at = time.Now().Add(-inOutStickyTTL - time.Second)
	c.m["ex1|ex2|BTC"] = e
	c.mu.Unlock()
	if _, _, ok := c.get("ex1", "ex2", "BTC"); ok {
		t.Errorf("expired entry should miss")
	}
}

func TestNextTsOf_Zero(t *testing.T) {
	if nextTsOf(time.Time{}) != 0 {
		t.Errorf("zero time should yield 0")
	}
}

func TestNextTsOf_EpochSeconds(t *testing.T) {
	ts := time.UnixMilli(1718000028000)
	got := nextTsOf(ts)
	if got != 1718000028 {
		t.Errorf("epoch seconds: %d", got)
	}
}

func TestIsListed_UnknownExchangePassesThrough(t *testing.T) {
	// Venues not in listedSources are NOT filtered → IsListed always true.
	if !IsListed("bybit", "BTC") {
		t.Errorf("bybit (no exchangeInfo source) should pass through")
	}
	if !IsListed("nonexistent", "ZZZ") {
		t.Errorf("unknown venue should pass through")
	}
}

func TestIsListed_FailOpenUntilFirstRefresh(t *testing.T) {
	// binance is in listedSources, but we haven't cached anything.
	// On a cold cache IsListed must FAIL-OPEN (return true) so a REST
	// outage doesn't blank the screener. The background goroutine
	// triggers a refresh — best-effort.
	if !IsListed("binance", "BTC") {
		t.Errorf("cold cache must fail-open (return true)")
	}
}
