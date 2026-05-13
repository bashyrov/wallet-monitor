package lighter

import (
	"testing"
	"time"
)

func newTestFutures() *Futures {
	m := newIDMap()
	m.mu.Lock()
	m.bySymb["BTC"] = 0
	m.bySymb["ETH"] = 1
	m.byID[0] = "BTC"
	m.byID[1] = "ETH"
	m.updated = time.Now()
	m.mu.Unlock()
	return &Futures{ids: m, books: make(map[int]*book)}
}

// Bug regression: original Lighter parser treated every push as a
// snapshot, so a 2-level delta would wipe out the 30-level book. Fix:
// type prefix "subscribed/" → snapshot (replace), anything else (i.e.
// "update/") → delta (merge in place).
func TestParse_SubscribedTypeReplacesBook(t *testing.T) {
	a := newTestFutures()
	// Pre-populate to verify replacement
	a.books[0] = &book{
		bids: map[float64]float64{99999: 1},
		asks: map[float64]float64{100001: 1},
	}
	frame := []byte(`{"type":"subscribed/order_book/0","channel":"order_book:0","order_book":{"bids":[{"price":"60000","size":"1.5"}],"asks":[{"price":"60100","size":"2.0"}]}}`)
	snap, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if snap == nil || snap.Symbol != "BTC" {
		t.Fatalf("snap: %+v", snap)
	}
	bk := a.books[0]
	if _, ok := bk.bids[99999]; ok {
		t.Errorf("subscribed/ type must wipe prior state (the regression bug)")
	}
	if bk.bids[60000] != 1.5 {
		t.Errorf("bid not seeded: %v", bk.bids[60000])
	}
}

func TestParse_UpdateTypeMergesDelta(t *testing.T) {
	a := newTestFutures()
	// Snapshot
	_, _ = a.Parse([]byte(`{"type":"subscribed/order_book/1","channel":"order_book:1","order_book":{"bids":[{"price":"3000","size":"5"}],"asks":[]}}`))
	// Delta — add a new level, must NOT wipe the snapshot
	_, _ = a.Parse([]byte(`{"type":"update/order_book/1","channel":"order_book:1","order_book":{"bids":[{"price":"2999","size":"10"}],"asks":[]}}`))
	bk := a.books[1]
	if bk.bids[3000] != 5 {
		t.Errorf("snapshot bid lost — original shrinkage bug: %v", bk.bids[3000])
	}
	if bk.bids[2999] != 10 {
		t.Errorf("delta bid: %v", bk.bids[2999])
	}
}

func TestParse_DeltaZeroSizeRemovesLevel(t *testing.T) {
	a := newTestFutures()
	_, _ = a.Parse([]byte(`{"type":"subscribed/order_book/0","channel":"order_book:0","order_book":{"bids":[{"price":"60000","size":"5"}],"asks":[]}}`))
	_, _ = a.Parse([]byte(`{"type":"update/order_book/0","channel":"order_book:0","order_book":{"bids":[{"price":"60000","size":"0"}],"asks":[]}}`))
	bk := a.books[0]
	if _, ok := bk.bids[60000]; ok {
		t.Errorf("size=0 should remove level, got %v", bk.bids[60000])
	}
}

func TestParse_BothChannelFormsAccepted(t *testing.T) {
	// Subscribe form uses "order_book/N"; echo form is "order_book:N". Both must parse.
	for _, ch := range []string{"order_book/0", "order_book:0"} {
		a := newTestFutures()
		frame := []byte(`{"type":"subscribed/order_book/0","channel":"` + ch + `","order_book":{"bids":[{"price":"60000","size":"1"}],"asks":[]}}`)
		snap, _ := a.Parse(frame)
		if snap == nil {
			t.Errorf("channel %q should parse, got nil", ch)
		}
	}
}

func TestParse_UnknownMarketIDDropped(t *testing.T) {
	a := newTestFutures()
	// market_id 999 not in idMap
	got, _ := a.Parse([]byte(`{"type":"subscribed/order_book/999","channel":"order_book:999","order_book":{"bids":[],"asks":[]}}`))
	if got != nil {
		t.Errorf("unknown market_id should produce nil, got %+v", got)
	}
}

func TestParse_NonOrderBookChannelIgnored(t *testing.T) {
	a := newTestFutures()
	got, _ := a.Parse([]byte(`{"type":"update/trade/0","channel":"trade:0","trades":[]}`))
	if got != nil {
		t.Errorf("non-order_book channel should produce nil, got %+v", got)
	}
}

func TestParse_MalformedIDDropped(t *testing.T) {
	a := newTestFutures()
	// "abc" can't parse as int
	got, _ := a.Parse([]byte(`{"type":"subscribed/order_book/abc","channel":"order_book:abc","order_book":{"bids":[],"asks":[]}}`))
	if got != nil {
		t.Errorf("malformed id should produce nil, got %+v", got)
	}
}
