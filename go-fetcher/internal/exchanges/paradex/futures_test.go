package paradex

import (
	"testing"
)

func newTestFutures() *Futures {
	return &Futures{books: make(map[string]*book)}
}

// Snapshot frame: update_type="s" + all resting orders in `inserts`.
// First frame after subscribe is always this shape.
func TestParse_SnapshotReplacesBook(t *testing.T) {
	a := newTestFutures()
	// Pre-populate to ensure snapshot wipes it.
	a.books["BTC"] = &book{
		bids: map[float64]float64{99999: 1},
		asks: map[float64]float64{100001: 1},
	}
	frame := []byte(`{"jsonrpc":"2.0","method":"subscription","params":{
		"channel":"order_book.BTC-USD-PERP.deltas",
		"data":{"market":"BTC-USD-PERP","update_type":"s",
			"inserts":[{"side":"BUY","price":"60000","size":"1.5"},{"side":"SELL","price":"60100","size":"2.0"}]
		}
	}}`)
	snap, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if snap == nil || snap.Symbol != "BTC" {
		t.Fatalf("snap: %+v", snap)
	}
	bk := a.books["BTC"]
	if _, ok := bk.bids[99999]; ok {
		t.Errorf("snapshot must wipe prior state — stale bid 99999 still present")
	}
	if bk.bids[60000] != 1.5 || bk.asks[60100] != 2.0 {
		t.Errorf("snapshot didn't populate: bids=%v asks=%v", bk.bids, bk.asks)
	}
}

func TestParse_DeltaInsertsAddLevels(t *testing.T) {
	a := newTestFutures()
	// Seed with snapshot
	_, _ = a.Parse([]byte(`{"method":"subscription","params":{"channel":"order_book.ETH-USD-PERP.deltas","data":{"market":"ETH-USD-PERP","update_type":"s","inserts":[{"side":"BUY","price":"3000","size":"5"}]}}}`))
	// Delta: insert new bid
	_, _ = a.Parse([]byte(`{"method":"subscription","params":{"channel":"order_book.ETH-USD-PERP.deltas","data":{"market":"ETH-USD-PERP","update_type":"d","inserts":[{"side":"BUY","price":"2999","size":"10"}]}}}`))
	bk := a.books["ETH"]
	if bk.bids[3000] != 5 {
		t.Errorf("snapshot bid lost: %v", bk.bids[3000])
	}
	if bk.bids[2999] != 10 {
		t.Errorf("delta insert: %v", bk.bids[2999])
	}
}

func TestParse_DeltaUpdatesModifyLevels(t *testing.T) {
	a := newTestFutures()
	_, _ = a.Parse([]byte(`{"method":"subscription","params":{"channel":"order_book.SOL-USD-PERP.deltas","data":{"market":"SOL-USD-PERP","update_type":"s","inserts":[{"side":"BUY","price":"150","size":"5"}]}}}`))
	// Update size of existing level
	_, _ = a.Parse([]byte(`{"method":"subscription","params":{"channel":"order_book.SOL-USD-PERP.deltas","data":{"market":"SOL-USD-PERP","update_type":"d","updates":[{"side":"BUY","price":"150","size":"8"}]}}}`))
	bk := a.books["SOL"]
	if bk.bids[150] != 8 {
		t.Errorf("update should change size to 8, got %v", bk.bids[150])
	}
}

func TestParse_DeltaDeletesRemoveLevels(t *testing.T) {
	a := newTestFutures()
	_, _ = a.Parse([]byte(`{"method":"subscription","params":{"channel":"order_book.BTC-USD-PERP.deltas","data":{"market":"BTC-USD-PERP","update_type":"s","inserts":[{"side":"BUY","price":"60000","size":"1"},{"side":"BUY","price":"59999","size":"2"}]}}}`))
	_, _ = a.Parse([]byte(`{"method":"subscription","params":{"channel":"order_book.BTC-USD-PERP.deltas","data":{"market":"BTC-USD-PERP","update_type":"d","deletes":[{"side":"BUY","price":"60000"}]}}}`))
	bk := a.books["BTC"]
	if _, ok := bk.bids[60000]; ok {
		t.Errorf("delete should remove 60000, still in book: %v", bk.bids[60000])
	}
	if bk.bids[59999] != 2 {
		t.Errorf("untouched level 59999 lost: %v", bk.bids[59999])
	}
}

func TestParse_NonSubscriptionMethodIgnored(t *testing.T) {
	a := newTestFutures()
	got, _ := a.Parse([]byte(`{"jsonrpc":"2.0","id":1,"result":{"channel":"order_book.BTC-USD-PERP.deltas"}}`))
	if got != nil {
		t.Errorf("subscribe result should produce nil, got %+v", got)
	}
}

func TestParse_NonPerpMarketIgnored(t *testing.T) {
	a := newTestFutures()
	got, _ := a.Parse([]byte(`{"method":"subscription","params":{"channel":"order_book.BTC-USDC","data":{"market":"BTC-USDC","update_type":"s","inserts":[]}}}`))
	if got != nil {
		t.Errorf("non-USD-PERP market should produce nil, got %+v", got)
	}
}

func TestParse_ZeroSizeInsertRemovesLevel(t *testing.T) {
	a := newTestFutures()
	_, _ = a.Parse([]byte(`{"method":"subscription","params":{"channel":"order_book.BTC-USD-PERP.deltas","data":{"market":"BTC-USD-PERP","update_type":"s","inserts":[{"side":"BUY","price":"60000","size":"5"}]}}}`))
	// Insert with size=0 is treated as remove (per code comment in apply())
	_, _ = a.Parse([]byte(`{"method":"subscription","params":{"channel":"order_book.BTC-USD-PERP.deltas","data":{"market":"BTC-USD-PERP","update_type":"d","inserts":[{"side":"BUY","price":"60000","size":"0"}]}}}`))
	bk := a.books["BTC"]
	if _, ok := bk.bids[60000]; ok {
		t.Errorf("size=0 insert should remove level, got %v", bk.bids[60000])
	}
}

func TestOnReconnect_ClearsBooks(t *testing.T) {
	a := newTestFutures()
	a.books["BTC"] = &book{bids: map[float64]float64{60000: 1}, asks: map[float64]float64{60100: 1}}
	a.OnReconnect()
	if len(a.books) != 0 {
		t.Errorf("OnReconnect must clear, got %d", len(a.books))
	}
}
