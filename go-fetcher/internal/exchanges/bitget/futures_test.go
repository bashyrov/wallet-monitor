package bitget

import (
	"testing"
)

func newTestFutures() *Adapter {
	return &Adapter{
		cacheKey: "bitget",
		instType: "USDT-FUTURES",
		books:    make(map[string]*book),
	}
}

func TestParse_SnapshotSeedsBook(t *testing.T) {
	a := newTestFutures()
	a.books["BTC"] = &book{
		bids: map[float64]float64{99999: 1},
		asks: map[float64]float64{100001: 1},
	}
	frame := []byte(`{"action":"snapshot","arg":{"instType":"USDT-FUTURES","channel":"books15","instId":"BTCUSDT"},"data":[{"bids":[["60000","1.5"]],"asks":[["60100","2.0"]]}]}`)
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
		t.Errorf("bid not set: %v", bk.bids[60000])
	}
}

func TestParse_UpdateActionApplyDelta(t *testing.T) {
	a := newTestFutures()
	_, _ = a.Parse([]byte(`{"action":"snapshot","arg":{"instType":"USDT-FUTURES","channel":"books15","instId":"ETHUSDT"},"data":[{"bids":[["3000","5"]],"asks":[]}]}`))
	_, _ = a.Parse([]byte(`{"action":"update","arg":{"instType":"USDT-FUTURES","channel":"books15","instId":"ETHUSDT"},"data":[{"bids":[["2999","10"]],"asks":[]}]}`))
	bk := a.books["ETH"]
	if bk.bids[3000] != 5 {
		t.Errorf("snapshot bid lost")
	}
	if bk.bids[2999] != 10 {
		t.Errorf("delta bid: %v", bk.bids[2999])
	}
}

func TestParse_WrongInstTypeIgnored(t *testing.T) {
	a := newTestFutures() // expects USDT-FUTURES
	// Spot leak — should be filtered
	got, _ := a.Parse([]byte(`{"action":"snapshot","arg":{"instType":"SPOT","channel":"books15","instId":"BTCUSDT"},"data":[{"bids":[["60000","1"]],"asks":[]}]}`))
	if got != nil {
		t.Errorf("wrong instType should produce nil, got %+v", got)
	}
}

func TestParse_NonBooks15ChannelIgnored(t *testing.T) {
	a := newTestFutures()
	got, _ := a.Parse([]byte(`{"action":"snapshot","arg":{"instType":"USDT-FUTURES","channel":"books1","instId":"BTCUSDT"},"data":[{"bids":[],"asks":[]}]}`))
	if got != nil {
		t.Errorf("books1 channel should produce nil, got %+v", got)
	}
}

func TestParse_EventFrameIgnored(t *testing.T) {
	a := newTestFutures()
	got, _ := a.Parse([]byte(`{"event":"subscribe","arg":{"instType":"USDT-FUTURES","channel":"books15","instId":"BTCUSDT"}}`))
	if got != nil {
		t.Errorf("event frame should produce nil, got %+v", got)
	}
}

func TestParse_SpotAdapterFiltersFutures(t *testing.T) {
	a := &Adapter{cacheKey: "bitget_spot", instType: "SPOT", books: make(map[string]*book)}
	// Spot adapter rejects USDT-FUTURES frames
	got, _ := a.Parse([]byte(`{"action":"snapshot","arg":{"instType":"USDT-FUTURES","channel":"books15","instId":"BTCUSDT"},"data":[]}`))
	if got != nil {
		t.Errorf("spot adapter must reject USDT-FUTURES, got %+v", got)
	}
	// And accepts SPOT
	snap, _ := a.Parse([]byte(`{"action":"snapshot","arg":{"instType":"SPOT","channel":"books15","instId":"BTCUSDT"},"data":[{"bids":[["60000","1"]],"asks":[]}]}`))
	if snap == nil || snap.Symbol != "BTC" {
		t.Errorf("spot adapter should accept SPOT frames, got %+v", snap)
	}
}
