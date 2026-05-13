package kraken

import (
	"testing"
)

// Kraken futures push-on-change book feed. Snapshot frame
// (feed="book_snapshot") seeds the book; subsequent
// per-level updates (feed="book") merge into it.
func TestParse_SnapshotEstablishesBook(t *testing.T) {
	a := &Futures{books: make(map[string]*book)}
	frame := []byte(`{"feed":"book_snapshot","product_id":"PF_XBTUSD","bids":[{"price":60000,"qty":1.5}],"asks":[{"price":60100,"qty":2.0}]}`)
	snap, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if snap == nil || snap.Symbol != "BTC" {
		t.Fatalf("snap: %+v (XBT should alias)", snap)
	}
	bk := a.books["BTC"]
	if bk.bids[60000] != 1.5 || bk.asks[60100] != 2.0 {
		t.Errorf("snapshot not seeded: %v / %v", bk.bids, bk.asks)
	}
}

func TestParse_DeltaApplyPerLevel(t *testing.T) {
	a := &Futures{books: make(map[string]*book)}
	_, _ = a.Parse([]byte(`{"feed":"book_snapshot","product_id":"PF_ETHUSD","bids":[{"price":3000,"qty":5}],"asks":[]}`))
	_, _ = a.Parse([]byte(`{"feed":"book","product_id":"PF_ETHUSD","side":"buy","price":2999,"qty":10}`))
	bk := a.books["ETH"]
	if bk.bids[3000] != 5 {
		t.Errorf("snapshot bid lost")
	}
	if bk.bids[2999] != 10 {
		t.Errorf("delta bid: %v", bk.bids[2999])
	}
}

func TestParse_ZeroQtyRemovesLevel(t *testing.T) {
	a := &Futures{books: make(map[string]*book)}
	_, _ = a.Parse([]byte(`{"feed":"book_snapshot","product_id":"PF_SOLUSD","bids":[{"price":150,"qty":5}],"asks":[]}`))
	_, _ = a.Parse([]byte(`{"feed":"book","product_id":"PF_SOLUSD","side":"buy","price":150,"qty":0}`))
	bk := a.books["SOL"]
	if _, ok := bk.bids[150]; ok {
		t.Errorf("qty=0 should remove, got %v", bk.bids[150])
	}
}

func TestParse_NonProductFrameIgnored(t *testing.T) {
	a := &Futures{books: make(map[string]*book)}
	got, _ := a.Parse([]byte(`{"event":"subscribed","feed":"book","product_ids":["PF_XBTUSD"]}`))
	if got != nil {
		t.Errorf("event frame should produce nil, got %+v", got)
	}
}

func TestParse_NonPFPrefixIgnored(t *testing.T) {
	a := &Futures{books: make(map[string]*book)}
	got, _ := a.Parse([]byte(`{"feed":"book_snapshot","product_id":"FI_XBTUSD_240329","bids":[],"asks":[]}`))
	if got != nil {
		t.Errorf("non-PF prefix should produce nil, got %+v", got)
	}
}

func TestParse_NonUSDSuffixIgnored(t *testing.T) {
	a := &Futures{books: make(map[string]*book)}
	got, _ := a.Parse([]byte(`{"feed":"book_snapshot","product_id":"PF_XBTEUR","bids":[],"asks":[]}`))
	if got != nil {
		t.Errorf("non-USD suffix should produce nil, got %+v", got)
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

func TestParse_DeltaUnknownSideIgnored(t *testing.T) {
	a := &Futures{books: make(map[string]*book)}
	// "side" must be "buy" or "sell"; anything else returns nil snap
	got, _ := a.Parse([]byte(`{"feed":"book","product_id":"PF_XBTUSD","side":"middle","price":60050,"qty":1}`))
	if got != nil {
		t.Errorf("unknown side should produce nil, got %+v", got)
	}
}
