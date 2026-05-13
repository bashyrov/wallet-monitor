package ticks

import "testing"

func mkTick(ex, sym string, id string, price float64) Tick {
	return Tick{Exchange: ex, Symbol: sym, Price: price, Size: 1.0, Side: Buy, TsMS: 1, ID: id}
}

func TestRing_PushAndRecent(t *testing.T) {
	r := NewRing(5)
	r.Push(mkTick("binance", "BTC", "1", 100))
	r.Push(mkTick("binance", "BTC", "2", 101))
	r.Push(mkTick("binance", "BTC", "3", 102))

	got := r.Recent("binance", "BTC", 0) // 0 = all
	if len(got) != 3 {
		t.Fatalf("len: want 3 got %d", len(got))
	}
	if got[0].ID != "1" || got[2].ID != "3" {
		t.Errorf("order: want [1,2,3] got %v", []string{got[0].ID, got[1].ID, got[2].ID})
	}
}

func TestRing_EvictsOnOverflow(t *testing.T) {
	r := NewRing(3)
	for i := 1; i <= 5; i++ {
		r.Push(mkTick("mexc", "ETH", string(rune('0'+i)), float64(i)))
	}
	got := r.Recent("mexc", "ETH", 0)
	if len(got) != 3 {
		t.Fatalf("len: want 3 got %d", len(got))
	}
	// expect ticks 3,4,5 (oldest two evicted)
	if got[0].ID != "3" || got[1].ID != "4" || got[2].ID != "5" {
		t.Errorf("eviction order: want [3,4,5] got [%s,%s,%s]", got[0].ID, got[1].ID, got[2].ID)
	}
}

func TestRing_RecentLimit(t *testing.T) {
	r := NewRing(10)
	for i := 1; i <= 7; i++ {
		r.Push(mkTick("okx", "SOL", string(rune('0'+i)), float64(i)))
	}
	got := r.Recent("okx", "SOL", 3)
	if len(got) != 3 {
		t.Fatalf("len: want 3 got %d", len(got))
	}
	// expect last 3: ticks 5,6,7
	if got[0].ID != "5" || got[2].ID != "7" {
		t.Errorf("recent(3): want [5,6,7] got [%s,%s,%s]", got[0].ID, got[1].ID, got[2].ID)
	}
}

func TestRing_RecentEmptyKey(t *testing.T) {
	r := NewRing(5)
	got := r.Recent("binance", "NONE", 0)
	if got == nil {
		t.Fatal("want non-nil empty slice, got nil")
	}
	if len(got) != 0 {
		t.Errorf("len: want 0 got %d", len(got))
	}
}

func TestRing_MultipleSymbolsIsolated(t *testing.T) {
	r := NewRing(5)
	r.Push(mkTick("binance", "BTC", "b1", 100))
	r.Push(mkTick("binance", "ETH", "e1", 200))
	r.Push(mkTick("binance", "BTC", "b2", 101))

	btc := r.Recent("binance", "BTC", 0)
	eth := r.Recent("binance", "ETH", 0)
	if len(btc) != 2 || len(eth) != 1 {
		t.Errorf("isolation: btc=%d eth=%d (want 2 and 1)", len(btc), len(eth))
	}
	if btc[0].ID != "b1" || btc[1].ID != "b2" || eth[0].ID != "e1" {
		t.Errorf("ids: btc=%v eth=%v", []string{btc[0].ID, btc[1].ID}, []string{eth[0].ID})
	}
}

func TestRing_ExchangeIsolation(t *testing.T) {
	r := NewRing(5)
	r.Push(mkTick("binance", "BTC", "b1", 100))
	r.Push(mkTick("mexc", "BTC", "m1", 100))

	binance := r.Recent("binance", "BTC", 0)
	mexc := r.Recent("mexc", "BTC", 0)
	if len(binance) != 1 || len(mexc) != 1 {
		t.Fatalf("exchange-isolation: binance=%d mexc=%d", len(binance), len(mexc))
	}
	if binance[0].ID != "b1" || mexc[0].ID != "m1" {
		t.Errorf("crossed: binance=%s mexc=%s", binance[0].ID, mexc[0].ID)
	}
}

func TestRing_DefaultCapWhenZero(t *testing.T) {
	r := NewRing(0)
	if r.cap != 50 {
		t.Errorf("default cap: want 50 got %d", r.cap)
	}
}

func TestRing_DefaultCapWhenNegative(t *testing.T) {
	r := NewRing(-3)
	if r.cap != 50 {
		t.Errorf("negative cap should default to 50, got %d", r.cap)
	}
}

func TestSide_StringValues(t *testing.T) {
	if Buy != "B" {
		t.Errorf("Buy: want B got %q", Buy)
	}
	if Sell != "S" {
		t.Errorf("Sell: want S got %q", Sell)
	}
}
