package whitebit

import (
	"testing"
)

// WhiteBIT depth_update params is positional [isFull bool, body {bids,asks}, market string].
// When isFull=true the book is replaced; otherwise deltas merge.
// Levels are [["price_string","size_string"],...].
func TestParse_FullSnapshotReplaces(t *testing.T) {
	a := &Futures{books: make(map[string]*book)}
	a.books["BTC"] = &book{
		bids: map[float64]float64{99999: 1},
		asks: map[float64]float64{100001: 1},
	}
	frame := []byte(`{"method":"depth_update","params":[true,{"bids":[["60000","1.5"]],"asks":[["60100","2.0"]]},"BTC_PERP"],"id":null}`)
	snap, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if snap == nil || snap.Symbol != "BTC" {
		t.Fatalf("snap: %+v", snap)
	}
	bk := a.books["BTC"]
	if _, ok := bk.bids[99999]; ok {
		t.Errorf("isFull=true must wipe prior state")
	}
	if bk.bids[60000] != 1.5 {
		t.Errorf("bid not seeded: %v", bk.bids[60000])
	}
}

func TestParse_DeltaMerge(t *testing.T) {
	a := &Futures{books: make(map[string]*book)}
	_, _ = a.Parse([]byte(`{"method":"depth_update","params":[true,{"bids":[["3000","5"]],"asks":[]},"ETH_PERP"]}`))
	_, _ = a.Parse([]byte(`{"method":"depth_update","params":[false,{"bids":[["2999","10"]],"asks":[]},"ETH_PERP"]}`))
	bk := a.books["ETH"]
	if bk.bids[3000] != 5 {
		t.Errorf("snapshot bid lost")
	}
	if bk.bids[2999] != 10 {
		t.Errorf("delta bid: %v", bk.bids[2999])
	}
}

func TestParse_ZeroSizeRemovesLevel(t *testing.T) {
	a := &Futures{books: make(map[string]*book)}
	_, _ = a.Parse([]byte(`{"method":"depth_update","params":[true,{"bids":[["150","5"]],"asks":[]},"SOL_PERP"]}`))
	_, _ = a.Parse([]byte(`{"method":"depth_update","params":[false,{"bids":[["150","0"]],"asks":[]},"SOL_PERP"]}`))
	bk := a.books["SOL"]
	if _, ok := bk.bids[150]; ok {
		t.Errorf("size=0 should remove level, got %v", bk.bids[150])
	}
}

func TestParse_NonDepthUpdateMethodIgnored(t *testing.T) {
	a := &Futures{books: make(map[string]*book)}
	got, _ := a.Parse([]byte(`{"method":"trades_update","params":["BTC_PERP",[]]}`))
	if got != nil {
		t.Errorf("non-depth_update should produce nil, got %+v", got)
	}
}

func TestParse_NonPerpMarketIgnored(t *testing.T) {
	a := &Futures{books: make(map[string]*book)}
	got, _ := a.Parse([]byte(`{"method":"depth_update","params":[true,{"bids":[],"asks":[]},"BTC_USDT"]}`))
	if got != nil {
		t.Errorf("non-_PERP market should produce nil, got %+v", got)
	}
}

func TestParse_SubscribeAckIgnored(t *testing.T) {
	a := &Futures{books: make(map[string]*book)}
	// Subscribe response has "result"/"error" — no "method" — should return nil
	got, _ := a.Parse([]byte(`{"id":1,"result":{"status":"success"},"error":null}`))
	if got != nil {
		t.Errorf("subscribe ack should produce nil, got %+v", got)
	}
}
