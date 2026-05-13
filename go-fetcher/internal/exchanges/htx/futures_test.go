package htx

import (
	"testing"
)

// HTX swap snapshot+delta state machine on
// market.<sym>-USDT.depth.size_20.high_freq channel.
// Snapshot establishes book; updates apply size=0 as remove.
func TestParse_SnapshotEstablishesBook(t *testing.T) {
	a := &Futures{books: make(map[string]*book)}
	a.books["BTC"] = &book{
		bids: map[float64]float64{99999: 1},
		asks: map[float64]float64{100001: 1},
	}
	frame := []byte(`{"ch":"market.BTC-USDT.depth.size_20.high_freq","tick":{"event":"snapshot","bids":[[60000,1.5]],"asks":[[60100,2.0]]}}`)
	snap, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if snap == nil || snap.Symbol != "BTC" {
		t.Fatalf("snap: %+v", snap)
	}
	bk := a.books["BTC"]
	if _, ok := bk.bids[99999]; ok {
		t.Errorf("snapshot must wipe prior state")
	}
	if bk.bids[60000] != 1.5 {
		t.Errorf("bid not seeded: %v", bk.bids[60000])
	}
}

func TestParse_UpdateEventAppliesDelta(t *testing.T) {
	a := &Futures{books: make(map[string]*book)}
	_, _ = a.Parse([]byte(`{"ch":"market.ETH-USDT.depth.size_20.high_freq","tick":{"event":"snapshot","bids":[[3000,5]],"asks":[]}}`))
	_, _ = a.Parse([]byte(`{"ch":"market.ETH-USDT.depth.size_20.high_freq","tick":{"event":"update","bids":[[2999,10]],"asks":[]}}`))
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
	_, _ = a.Parse([]byte(`{"ch":"market.SOL-USDT.depth.size_20.high_freq","tick":{"event":"snapshot","bids":[[150,5]],"asks":[]}}`))
	_, _ = a.Parse([]byte(`{"ch":"market.SOL-USDT.depth.size_20.high_freq","tick":{"event":"update","bids":[[150,0]],"asks":[]}}`))
	bk := a.books["SOL"]
	if _, ok := bk.bids[150]; ok {
		t.Errorf("size=0 should remove level, got %v", bk.bids[150])
	}
}

func TestParse_NonDepthChannelIgnored(t *testing.T) {
	a := &Futures{books: make(map[string]*book)}
	got, _ := a.Parse([]byte(`{"ch":"market.BTC-USDT.trade.detail","tick":{}}`))
	if got != nil {
		t.Errorf("trade channel should produce nil, got %+v", got)
	}
}

func TestParse_NonUSDTPairIgnored(t *testing.T) {
	a := &Futures{books: make(map[string]*book)}
	got, _ := a.Parse([]byte(`{"ch":"market.BTC-USDC.depth.size_20.high_freq","tick":{"event":"snapshot","bids":[],"asks":[]}}`))
	if got != nil {
		t.Errorf("non-USDT pair should produce nil, got %+v", got)
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

// PongFor: HTX sends {"ping": N} and expects {"pong": N} reply with
// preserved number type (sonic round-trip). String type kicks the conn.
func TestPongFor_RepliesWithIntegerPong(t *testing.T) {
	a := &Futures{}
	reply := a.PongFor([]byte(`{"ping":1718000001234}`))
	if reply == nil {
		t.Fatal("PongFor returned nil for valid ping")
	}
	// Verify the reply preserves the int type — must contain literal ":1718000001234"
	// without quotes around the number.
	want := `{"pong":1718000001234}`
	if string(reply) != want {
		t.Errorf("PongFor: want %q got %q", want, string(reply))
	}
}

func TestPongFor_IgnoresNonPing(t *testing.T) {
	a := &Futures{}
	if a.PongFor([]byte(`{"ch":"market.BTC-USDT.depth"}`)) != nil {
		t.Error("PongFor should return nil for non-ping frame")
	}
}
