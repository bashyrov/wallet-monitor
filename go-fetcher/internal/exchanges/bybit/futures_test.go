package bybit

import (
	"testing"
)

func newTestFutures() *Futures {
	return &Futures{books: make(map[string]*book)}
}

func TestParse_SnapshotEstablishesBook(t *testing.T) {
	a := newTestFutures()
	frame := []byte(`{"topic":"orderbook.50.BTCUSDT","type":"snapshot","ts":1718000001000,"data":{"s":"BTCUSDT","b":[["60000","1.5"]],"a":[["60100","2.0"]]}}`)
	snap, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if snap == nil || snap.Symbol != "BTC" {
		t.Fatalf("snapshot: %+v", snap)
	}
	if len(snap.Bids) != 1 || snap.Bids[0][0] != 60000 || snap.Bids[0][1] != 1.5 {
		t.Errorf("bids: %v", snap.Bids)
	}
}

func TestParse_DeltaAppliesIncrement(t *testing.T) {
	a := newTestFutures()
	// snapshot
	_, _ = a.Parse([]byte(`{"topic":"orderbook.50.ETHUSDT","type":"snapshot","data":{"s":"ETHUSDT","b":[["3000","5"]],"a":[["3001","2"]]}}`))
	// delta: add bid level
	_, err := a.Parse([]byte(`{"topic":"orderbook.50.ETHUSDT","type":"delta","data":{"s":"ETHUSDT","b":[["2999","10"]],"a":[]}}`))
	if err != nil {
		t.Fatalf("delta: %v", err)
	}
	bk := a.books["ETH"]
	if bk.bids[3000] != 5 {
		t.Errorf("snapshot bid lost: %v", bk.bids[3000])
	}
	if bk.bids[2999] != 10 {
		t.Errorf("delta bid: %v", bk.bids[2999])
	}
}

func TestParse_DeltaZeroSizeRemovesLevel(t *testing.T) {
	a := newTestFutures()
	_, _ = a.Parse([]byte(`{"topic":"orderbook.50.SOLUSDT","type":"snapshot","data":{"s":"SOLUSDT","b":[["150","5"]],"a":[]}}`))
	// Bybit signals removal with size="0"
	_, _ = a.Parse([]byte(`{"topic":"orderbook.50.SOLUSDT","type":"delta","data":{"s":"SOLUSDT","b":[["150","0"]],"a":[]}}`))
	bk := a.books["SOL"]
	if _, ok := bk.bids[150]; ok {
		t.Errorf("size=0 must remove level, bk.bids[150] still %v", bk.bids[150])
	}
}

func TestParse_SnapshotReplacesPriorState(t *testing.T) {
	a := newTestFutures()
	_, _ = a.Parse([]byte(`{"topic":"orderbook.50.BTCUSDT","type":"snapshot","data":{"s":"BTCUSDT","b":[["59999","1"]],"a":[]}}`))
	// Reconnect-like fresh snapshot
	_, _ = a.Parse([]byte(`{"topic":"orderbook.50.BTCUSDT","type":"snapshot","data":{"s":"BTCUSDT","b":[["60000","2"]],"a":[]}}`))
	bk := a.books["BTC"]
	if _, ok := bk.bids[59999]; ok {
		t.Errorf("re-snapshot must wipe prior state; 59999 still in book: %v", bk.bids[59999])
	}
	if bk.bids[60000] != 2 {
		t.Errorf("new snapshot bid: %v", bk.bids[60000])
	}
}

func TestParse_PongIgnored(t *testing.T) {
	a := newTestFutures()
	got, _ := a.Parse([]byte(`{"op":"pong","success":true}`))
	if got != nil {
		t.Errorf("pong should produce nil, got %+v", got)
	}
}

func TestParse_SubscribeAckIgnored(t *testing.T) {
	a := newTestFutures()
	got, _ := a.Parse([]byte(`{"op":"subscribe","success":true,"retMsg":"ok"}`))
	if got != nil {
		t.Errorf("subscribe ack should produce nil, got %+v", got)
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
