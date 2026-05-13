package funding

import (
	"sync"
	"testing"
	"time"
)

func TestStore_ApplyCreatesEntry(t *testing.T) {
	s := NewStore()
	s.Apply("binance", Tick{Symbol: "BTC", Rate: 0.0001, MarkPrice: 60000})
	got, ok := s.Get("binance", "BTC")
	if !ok {
		t.Fatal("Get miss after Apply")
	}
	if got.Rate != 0.0001 || got.MarkPrice != 60000 {
		t.Errorf("entry: %+v", got)
	}
	if got.UpdatedAt.IsZero() {
		t.Errorf("UpdatedAt should auto-populate to now()")
	}
}

func TestStore_ApplyIgnoresEmptySymbol(t *testing.T) {
	s := NewStore()
	s.Apply("binance", Tick{Rate: 0.0001})
	if _, ok := s.Get("binance", ""); ok {
		t.Errorf("empty symbol should NOT create entry")
	}
}

// Bug class #7 regression: WS pushes that omit volume must NOT wipe the
// volume the REST backstop set previously. Merge strategy is "overwrite
// only if new value is non-zero".
func TestStore_ApplyNonZeroOverwriteMergePolicy(t *testing.T) {
	s := NewStore()
	// First: REST backstop sweep — fills volume + open interest + interval
	s.Apply("bybit", Tick{
		Symbol:     "BTC",
		Rate:       0.0001,
		MarkPrice:  60000,
		Volume24h:  1e9,
		OpenIntUSD: 5e8,
		IntervalH:  8,
	})
	// Then: WS push — only carries rate + mark (volume/OI omitted = 0)
	s.Apply("bybit", Tick{Symbol: "BTC", Rate: 0.00015, MarkPrice: 60100})

	got, _ := s.Get("bybit", "BTC")
	if got.Rate != 0.00015 {
		t.Errorf("rate should be updated: %v", got.Rate)
	}
	if got.MarkPrice != 60100 {
		t.Errorf("mark should be updated: %v", got.MarkPrice)
	}
	if got.Volume24h != 1e9 {
		t.Errorf("VOLUME WIPE REGRESSION — WS push must NOT zero volume, got %v", got.Volume24h)
	}
	if got.OpenIntUSD != 5e8 {
		t.Errorf("OpenInt wiped: %v", got.OpenIntUSD)
	}
	if got.IntervalH != 8 {
		t.Errorf("IntervalH wiped: %v", got.IntervalH)
	}
}

func TestStore_ApplyZeroRateDoesNotOverwrite(t *testing.T) {
	s := NewStore()
	s.Apply("binance", Tick{Symbol: "BTC", Rate: 0.0001})
	s.Apply("binance", Tick{Symbol: "BTC", Rate: 0, MarkPrice: 60000})
	got, _ := s.Get("binance", "BTC")
	if got.Rate != 0.0001 {
		t.Errorf("zero rate should not overwrite: got %v", got.Rate)
	}
}

func TestStore_ApplyZeroNextFundingDoesNotOverwrite(t *testing.T) {
	s := NewStore()
	ts := time.UnixMilli(1718000028000)
	s.Apply("binance", Tick{Symbol: "BTC", Rate: 0.0001, NextFunding: ts})
	// Second apply with zero NextFunding — must preserve
	s.Apply("binance", Tick{Symbol: "BTC", Rate: 0.0002})
	got, _ := s.Get("binance", "BTC")
	if !got.NextFunding.Equal(ts) {
		t.Errorf("NextFunding wiped: %v vs %v", got.NextFunding, ts)
	}
}

func TestStore_ApplyAutoPopulatesUpdatedAt(t *testing.T) {
	s := NewStore()
	before := time.Now()
	s.Apply("binance", Tick{Symbol: "BTC", Rate: 0.0001})
	got, _ := s.Get("binance", "BTC")
	if got.UpdatedAt.Before(before) {
		t.Errorf("UpdatedAt should be ≥ now, got %v vs %v", got.UpdatedAt, before)
	}
}

func TestStore_ApplyPreservesExplicitUpdatedAt(t *testing.T) {
	s := NewStore()
	ts := time.UnixMilli(1718000001000)
	s.Apply("binance", Tick{Symbol: "BTC", Rate: 0.0001, UpdatedAt: ts})
	got, _ := s.Get("binance", "BTC")
	if !got.UpdatedAt.Equal(ts) {
		t.Errorf("explicit UpdatedAt overwritten: %v", got.UpdatedAt)
	}
}

func TestStore_GetMissReturnsFalse(t *testing.T) {
	s := NewStore()
	if _, ok := s.Get("binance", "NONE"); ok {
		t.Errorf("Get miss should return false")
	}
}

func TestStore_SnapshotByExchangeBuckets(t *testing.T) {
	s := NewStore()
	s.Apply("binance", Tick{Symbol: "BTC", Rate: 0.0001})
	s.Apply("binance", Tick{Symbol: "ETH", Rate: 0.0002})
	s.Apply("bybit", Tick{Symbol: "BTC", Rate: 0.0003})

	snap := s.SnapshotByExchange()
	if len(snap) != 2 {
		t.Fatalf("exchanges: want 2 got %d", len(snap))
	}
	if len(snap["binance"]) != 2 {
		t.Errorf("binance bucket: want 2 got %d", len(snap["binance"]))
	}
	if len(snap["bybit"]) != 1 {
		t.Errorf("bybit bucket: %d", len(snap["bybit"]))
	}
	if snap["binance"]["BTC"].Rate != 0.0001 {
		t.Errorf("nested entry: %+v", snap["binance"]["BTC"])
	}
}

func TestStore_SnapshotByExchangeReturnsCopies(t *testing.T) {
	s := NewStore()
	s.Apply("binance", Tick{Symbol: "BTC", Rate: 0.0001})
	snap := s.SnapshotByExchange()
	snap["binance"]["BTC"] = Tick{Symbol: "BTC", Rate: 9999} // mutate the copy
	// Original should be unchanged
	got, _ := s.Get("binance", "BTC")
	if got.Rate != 0.0001 {
		t.Errorf("snapshot mutation leaked back: %v", got.Rate)
	}
}

func TestStore_ConcurrentApplyAndGetSafe(t *testing.T) {
	s := NewStore()
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		for i := 0; i < 200; i++ {
			s.Apply("binance", Tick{Symbol: "BTC", Rate: 0.0001, MarkPrice: float64(60000 + i)})
		}
	}()
	go func() {
		defer wg.Done()
		for i := 0; i < 200; i++ {
			_, _ = s.Get("binance", "BTC")
		}
	}()
	wg.Wait()
}

func TestParseFloat_NumberAndString(t *testing.T) {
	// ParseFloat handles both JSON number (decoded as float64) and JSON
	// string (decoded as string). Used by venues like Paradex/Backpack
	// that wrap numbers as strings.
	if v := ParseFloat(float64(42.5)); v != 42.5 {
		t.Errorf("float64: %v", v)
	}
	if v := ParseFloat("42.5"); v != 42.5 {
		t.Errorf("string: %v", v)
	}
	if v := ParseFloat("not a number"); v != 0 {
		t.Errorf("garbage string should be 0: %v", v)
	}
	if v := ParseFloat(nil); v != 0 {
		t.Errorf("nil: %v", v)
	}
}
