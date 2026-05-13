package okx

import (
	"testing"
)

func newTestFutures() *Futures {
	return &Futures{
		cacheKey:   "okx",
		instSuffix: "-USDT-SWAP",
		books:      make(map[string]*book),
	}
}

func TestParse_SnapshotEstablishesBook(t *testing.T) {
	a := newTestFutures()
	// OKX rows are 4-element [px, sz, "0", numOrders]; first 2 are price/size.
	frame := []byte(`{"arg":{"channel":"books","instId":"BTC-USDT-SWAP"},"action":"snapshot","data":[{"bids":[["60000","1.5","0","3"]],"asks":[["60100","2.0","0","4"]]}]}`)
	snap, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if snap == nil || snap.Symbol != "BTC" {
		t.Fatalf("snap: %+v", snap)
	}
	bk := a.books["BTC"]
	if bk.bids[60000] != 1.5 || bk.asks[60100] != 2.0 {
		t.Errorf("snapshot not seeded: bids=%v asks=%v", bk.bids, bk.asks)
	}
}

func TestParse_UpdateActionAppliesDelta(t *testing.T) {
	a := newTestFutures()
	_, _ = a.Parse([]byte(`{"arg":{"channel":"books","instId":"ETH-USDT-SWAP"},"action":"snapshot","data":[{"bids":[["3000","5","0","1"]],"asks":[]}]}`))
	_, _ = a.Parse([]byte(`{"arg":{"channel":"books","instId":"ETH-USDT-SWAP"},"action":"update","data":[{"bids":[["2999","10","0","2"]],"asks":[]}]}`))
	bk := a.books["ETH"]
	if bk.bids[3000] != 5 {
		t.Errorf("snapshot bid lost: %v", bk.bids[3000])
	}
	if bk.bids[2999] != 10 {
		t.Errorf("delta bid: %v", bk.bids[2999])
	}
}

func TestParse_ZeroSizeRemovesLevel(t *testing.T) {
	a := newTestFutures()
	_, _ = a.Parse([]byte(`{"arg":{"channel":"books","instId":"SOL-USDT-SWAP"},"action":"snapshot","data":[{"bids":[["150","5","0","1"]],"asks":[]}]}`))
	_, _ = a.Parse([]byte(`{"arg":{"channel":"books","instId":"SOL-USDT-SWAP"},"action":"update","data":[{"bids":[["150","0","0","0"]],"asks":[]}]}`))
	bk := a.books["SOL"]
	if _, ok := bk.bids[150]; ok {
		t.Errorf("size=0 should remove level, still in book: %v", bk.bids[150])
	}
}

func TestParse_SubscribeEventIgnored(t *testing.T) {
	a := newTestFutures()
	got, _ := a.Parse([]byte(`{"event":"subscribe","arg":{"channel":"books","instId":"BTC-USDT-SWAP"}}`))
	if got != nil {
		t.Errorf("event frame should produce nil, got %+v", got)
	}
}

func TestParse_NonBooksChannelIgnored(t *testing.T) {
	a := newTestFutures()
	got, _ := a.Parse([]byte(`{"arg":{"channel":"bbo-tbt","instId":"BTC-USDT-SWAP"},"data":[]}`))
	if got != nil {
		t.Errorf("non-books channel should produce nil, got %+v", got)
	}
}

func TestParse_WrongInstSuffixIgnored(t *testing.T) {
	a := newTestFutures() // expects -USDT-SWAP
	// Spot frame leaks through wouldn't match suffix
	got, _ := a.Parse([]byte(`{"arg":{"channel":"books","instId":"BTC-USDT"},"action":"snapshot","data":[{"bids":[],"asks":[]}]}`))
	if got != nil {
		t.Errorf("wrong inst suffix should produce nil, got %+v", got)
	}
}

func TestParse_NewSpotAdapterTakesUSDTSuffix(t *testing.T) {
	// Spot adapter ignores -USDT-SWAP frames and accepts -USDT
	a := &Futures{cacheKey: "okx_spot", instSuffix: "-USDT", books: make(map[string]*book)}
	snap, _ := a.Parse([]byte(`{"arg":{"channel":"books","instId":"BTC-USDT"},"action":"snapshot","data":[{"bids":[["60000","1","0","1"]],"asks":[["60100","1","0","1"]]}]}`))
	if snap == nil || snap.Symbol != "BTC" {
		t.Errorf("spot adapter should parse -USDT, got %+v", snap)
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
