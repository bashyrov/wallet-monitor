package mexc

import (
	"net/http"
	"testing"
	"time"
)

func newTestFutures() *Futures {
	return &Futures{
		books: make(map[string]*book),
		http:  &http.Client{Timeout: time.Second},
	}
}

func TestParse_DepthFullReplacesBook(t *testing.T) {
	a := newTestFutures()
	// Pre-populate to ensure full-replace wipes it.
	a.books["BTC"] = &book{
		bids: map[float64]float64{99999: 1},
		asks: map[float64]float64{100001: 1},
	}
	// MEXC sub.depth.full publishes on channel "push.depth.full" with
	// arrays of [px, sz, n].
	frame := []byte(`{"channel":"push.depth.full","symbol":"BTC_USDT","data":{"bids":[[60000,1.5,3],[59999,2.0,5]],"asks":[[60100,2.0,4]]}}`)
	snap, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if snap == nil || snap.Symbol != "BTC" {
		t.Fatalf("snap: %+v", snap)
	}
	bk := a.books["BTC"]
	if _, ok := bk.bids[99999]; ok {
		t.Errorf("full-replace must wipe stale 99999")
	}
	if bk.bids[60000] != 1.5 || bk.asks[60100] != 2.0 {
		t.Errorf("snapshot not seeded: bids=%v asks=%v", bk.bids, bk.asks)
	}
}

func TestParse_LegacyPushDepthAccepted(t *testing.T) {
	// We accept push.depth.full (current) AND push.depth (legacy) per code comment.
	a := newTestFutures()
	_, err := a.Parse([]byte(`{"channel":"push.depth","symbol":"ETH_USDT","data":{"bids":[[3000,5,1]],"asks":[]}}`))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if _, ok := a.books["ETH"]; !ok {
		t.Errorf("legacy push.depth must still seed the book")
	}
}

func TestParse_NonDepthChannelIgnored(t *testing.T) {
	a := newTestFutures()
	got, _ := a.Parse([]byte(`{"channel":"push.deal","symbol":"BTC_USDT","data":[{"p":60000}]}`))
	if got != nil {
		t.Errorf("push.deal should produce nil, got %+v", got)
	}
}

func TestParse_NonUSDTSymbolIgnored(t *testing.T) {
	a := newTestFutures()
	got, _ := a.Parse([]byte(`{"channel":"push.depth.full","symbol":"BTC_USDC","data":{"bids":[[60000,1]],"asks":[]}}`))
	if got != nil {
		t.Errorf("non-_USDT symbol should produce nil, got %+v", got)
	}
}

func TestParse_ZeroSizeRowFiltered(t *testing.T) {
	a := newTestFutures()
	// sub.depth.full uses full-replace, so a sz=0 row simply doesn't enter the book.
	_, _ = a.Parse([]byte(`{"channel":"push.depth.full","symbol":"BTC_USDT","data":{"bids":[[60000,0,0],[59999,1,1]],"asks":[]}}`))
	bk := a.books["BTC"]
	if _, ok := bk.bids[60000]; ok {
		t.Errorf("sz=0 row must NOT enter book, got bids[60000]=%v", bk.bids[60000])
	}
	if bk.bids[59999] != 1 {
		t.Errorf("legit row lost: %v", bk.bids[59999])
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
