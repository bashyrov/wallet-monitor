package backpack

import (
	"testing"
)

// Backpack depth stream is DIFF-only — full book must be REST-seeded
// then merged. Deltas arrive single-level per push. size=0 removes
// the level.
func TestParse_DeltaApplyTopLevels(t *testing.T) {
	a := &Futures{books: make(map[string]*book)}
	frame := []byte(`{"stream":"depth.BTC_USDC_PERP","data":{"e":"depth","E":1718000001000,"s":"BTC_USDC_PERP","b":[["60000","1.5"]],"a":[["60100","2.0"]],"u":42}}`)
	snap, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if snap == nil || snap.Symbol != "BTC" {
		t.Fatalf("snap: %+v", snap)
	}
	bk := a.books["BTC"]
	if bk.bids[60000] != 1.5 || bk.asks[60100] != 2.0 {
		t.Errorf("delta not applied: bids=%v asks=%v", bk.bids, bk.asks)
	}
}

func TestParse_DeltaMergeWithPriorState(t *testing.T) {
	a := &Futures{books: make(map[string]*book)}
	_, _ = a.Parse([]byte(`{"stream":"depth.ETH_USDC_PERP","data":{"s":"ETH_USDC_PERP","b":[["3000","5"]],"a":[]}}`))
	// Second delta: add new bid level, keep old one
	_, _ = a.Parse([]byte(`{"stream":"depth.ETH_USDC_PERP","data":{"s":"ETH_USDC_PERP","b":[["2999","10"]],"a":[]}}`))
	bk := a.books["ETH"]
	if bk.bids[3000] != 5 {
		t.Errorf("prior bid lost")
	}
	if bk.bids[2999] != 10 {
		t.Errorf("new bid: %v", bk.bids[2999])
	}
}

func TestParse_ZeroSizeRemovesLevel(t *testing.T) {
	a := &Futures{books: make(map[string]*book)}
	_, _ = a.Parse([]byte(`{"stream":"depth.SOL_USDC_PERP","data":{"s":"SOL_USDC_PERP","b":[["150","5"]],"a":[]}}`))
	_, _ = a.Parse([]byte(`{"stream":"depth.SOL_USDC_PERP","data":{"s":"SOL_USDC_PERP","b":[["150","0"]],"a":[]}}`))
	bk := a.books["SOL"]
	if _, ok := bk.bids[150]; ok {
		t.Errorf("size=0 should remove level, got %v", bk.bids[150])
	}
}

func TestParse_NonDepthStreamIgnored(t *testing.T) {
	a := &Futures{books: make(map[string]*book)}
	got, _ := a.Parse([]byte(`{"stream":"trade.BTC_USDC_PERP","data":{}}`))
	if got != nil {
		t.Errorf("non-depth stream should produce nil, got %+v", got)
	}
}

func TestParse_NonPerpSymbolIgnored(t *testing.T) {
	a := &Futures{books: make(map[string]*book)}
	got, _ := a.Parse([]byte(`{"stream":"depth.BTC_USDC","data":{"s":"BTC_USDC","b":[["60000","1"]],"a":[]}}`))
	if got != nil {
		t.Errorf("non-_USDC_PERP suffix should produce nil, got %+v", got)
	}
}

func TestOnReconnect_ClearsBooks(t *testing.T) {
	a := &Futures{books: make(map[string]*book)}
	a.books["BTC"] = &book{bids: map[float64]float64{60000: 1}, asks: map[float64]float64{60100: 1}}
	a.OnReconnect()
	if len(a.books) != 0 {
		t.Errorf("OnReconnect must clear, got %d", len(a.books))
	}
}
