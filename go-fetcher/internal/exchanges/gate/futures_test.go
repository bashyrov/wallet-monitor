package gate

import (
	"testing"
)

func newTestFutures() *Futures {
	return &Futures{books: make(map[string]*book)}
}

func TestParse_AllEventReplacesBook(t *testing.T) {
	a := newTestFutures()
	// Pre-populate state to ensure "all" wipes it.
	a.books["BTC"] = &book{
		bids: map[float64]float64{99999: 1},
		asks: map[float64]float64{100001: 1},
	}
	frame := []byte(`{"channel":"futures.order_book","event":"all","result":{"contract":"BTC_USDT","bids":[{"p":"60000","s":1.5}],"asks":[{"p":"60100","s":2.0}]}}`)
	snap, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if snap == nil {
		t.Fatal("snapshot nil")
	}
	if snap.Symbol != "BTC" {
		t.Errorf("symbol: %s", snap.Symbol)
	}
	bk := a.books["BTC"]
	if _, ok := bk.bids[99999]; ok {
		t.Errorf("'all' must wipe prior state — stale bid at 99999 still present")
	}
	if bk.bids[60000] != 1.5 {
		t.Errorf("new bid 60000@1.5: got %v", bk.bids[60000])
	}
}

func TestParse_UpdateEventApplyDelta(t *testing.T) {
	a := newTestFutures()
	// Seed with snapshot
	_, _ = a.Parse([]byte(`{"channel":"futures.order_book","event":"all","result":{"contract":"ETH_USDT","bids":[{"p":"3000","s":5}],"asks":[{"p":"3001","s":2}]}}`))
	// Delta: add new bid level
	_, err := a.Parse([]byte(`{"channel":"futures.order_book","event":"update","result":{"contract":"ETH_USDT","bids":[{"p":"2999","s":10}],"asks":[]}}`))
	if err != nil {
		t.Fatalf("delta parse: %v", err)
	}
	bk := a.books["ETH"]
	if bk.bids[3000] != 5 {
		t.Errorf("snapshot bid lost: bk.bids[3000]=%v", bk.bids[3000])
	}
	if bk.bids[2999] != 10 {
		t.Errorf("delta bid not applied: bk.bids[2999]=%v", bk.bids[2999])
	}
}

func TestParse_DeltaZeroSizeRemovesLevel(t *testing.T) {
	a := newTestFutures()
	_, _ = a.Parse([]byte(`{"channel":"futures.order_book","event":"all","result":{"contract":"SOL_USDT","bids":[{"p":"150","s":5}],"asks":[{"p":"151","s":2}]}}`))
	// size=0 → remove level
	_, _ = a.Parse([]byte(`{"channel":"futures.order_book","event":"update","result":{"contract":"SOL_USDT","bids":[{"p":"150","s":0}],"asks":[]}}`))
	bk := a.books["SOL"]
	if _, ok := bk.bids[150]; ok {
		t.Errorf("size=0 should remove level, bk.bids[150] still present: %v", bk.bids[150])
	}
}

func TestParse_SubscribeAckIgnored(t *testing.T) {
	a := newTestFutures()
	got, _ := a.Parse([]byte(`{"channel":"futures.order_book","event":"subscribe","result":{"status":"success"}}`))
	if got != nil {
		t.Errorf("subscribe ack should produce nil, got %v", got)
	}
}

func TestParse_NonOrderBookChannelIgnored(t *testing.T) {
	a := newTestFutures()
	got, _ := a.Parse([]byte(`{"channel":"futures.trades","event":"update","result":[]}`))
	if got != nil {
		t.Errorf("non-orderbook channel should produce nil, got %v", got)
	}
}

func TestParse_NonUSDTContractIgnored(t *testing.T) {
	a := newTestFutures()
	got, _ := a.Parse([]byte(`{"channel":"futures.order_book","event":"all","result":{"contract":"BTC_USDC","bids":[],"asks":[]}}`))
	if got != nil {
		t.Errorf("non-_USDT contract should produce nil, got %v", got)
	}
}

func TestOnReconnect_ClearsBooks(t *testing.T) {
	a := newTestFutures()
	a.books["BTC"] = &book{bids: map[float64]float64{60000: 1}, asks: map[float64]float64{60100: 1}}
	a.OnReconnect()
	if len(a.books) != 0 {
		t.Errorf("OnReconnect must clear books, got %d entries", len(a.books))
	}
}
