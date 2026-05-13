package canonical

import "testing"

func TestLimit_RoundsUpToCanonical(t *testing.T) {
	// Binance valid: {5, 10, 20, 50, 100, 500, 1000}
	cases := []struct {
		ex       string
		req      int
		want     int
	}{
		{"binance", 12, 20},  // 12 → next valid 20
		{"binance", 5, 5},    // exact match
		{"binance", 1, 5},    // below smallest → smallest
		{"binance", 100, 100}, // exact
		{"binance", 101, 500}, // next valid
		{"bybit", 30, 50},    // bybit set {1,50,200,500,1000}
		{"okx", 3, 5},        // okx {1,5,10,20,50,100,200,400}
	}
	for _, c := range cases {
		got := Limit(c.ex, c.req)
		if got != c.want {
			t.Errorf("Limit(%q, %d): want %d got %d", c.ex, c.req, c.want, got)
		}
	}
}

func TestLimit_ClampsToCapWhenExceeded(t *testing.T) {
	// requested > largest valid → clamp to the cap, not 0.
	got := Limit("gate", 200) // gate set {5,10,20,50,100}, max=100
	if got != 100 {
		t.Errorf("clamp: want 100 got %d", got)
	}
	got2 := Limit("binance", 9999) // max=1000
	if got2 != 1000 {
		t.Errorf("clamp binance: want 1000 got %d", got2)
	}
}

func TestLimit_UnknownExchangeReturnsRequested(t *testing.T) {
	// No canonical set → return as-is.
	if got := Limit("kucoin", 47); got != 47 {
		t.Errorf("unknown exchange: want 47 got %d", got)
	}
	if got := Limit("nonexistent", 100); got != 100 {
		t.Errorf("unknown exchange: want 100 got %d", got)
	}
}

func TestLimit_SpotSetDiffersFromFutures(t *testing.T) {
	// binance_spot supports 5000; binance fut caps at 1000.
	if got := Limit("binance_spot", 5000); got != 5000 {
		t.Errorf("binance_spot 5000: want 5000 got %d", got)
	}
	if got := Limit("binance", 5000); got != 1000 {
		t.Errorf("binance 5000 should clamp to 1000, got %d", got)
	}
}

func TestLimit_BitgetSpotIncludes1(t *testing.T) {
	// bitget_spot starts at 1; bitget fut starts at 5.
	if got := Limit("bitget_spot", 1); got != 1 {
		t.Errorf("bitget_spot 1: want 1 got %d", got)
	}
	if got := Limit("bitget", 1); got != 5 {
		t.Errorf("bitget 1 should round up to 5, got %d", got)
	}
}

func TestLimit_AsterSameAsBinance(t *testing.T) {
	// Aster is documented as a Binance fork — should accept same set.
	for _, n := range []int{5, 12, 100, 500, 1000} {
		want := Limit("binance", n)
		got := Limit("aster", n)
		if got != want {
			t.Errorf("aster(%d)=%d should match binance(%d)=%d", n, got, n, want)
		}
	}
}
