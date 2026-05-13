package kucoin

import (
	"testing"
)

func newTestFutures() *Futures {
	return &Futures{books: make(map[string]*book)}
}

// KuCoin level2Depth50 pushes full top-50 snapshot every ~100ms.
// Each frame is stateless (Parse returns from frame data directly,
// adapter doesn't maintain state across frames — confirmed by reading
// futures.go: no map mutation, parseSide builds fresh slice).
func TestParse_FullSnapshotFromFrame(t *testing.T) {
	a := newTestFutures()
	// KuCoin bid/ask rows are [price_string, size_number] (mixed-type slice → [][]any).
	frame := []byte(`{"type":"message","topic":"/contractMarket/level2Depth50:XBTUSDTM","subject":"level2","data":{"bids":[["60000",1.5],["59999",2.0]],"asks":[["60100",2.0],["60101",1.0]]}}`)
	snap, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if snap == nil || snap.Symbol != "BTC" { // XBT alias
		t.Fatalf("snap: %+v (XBT should alias to BTC)", snap)
	}
	if len(snap.Bids) != 2 || snap.Bids[0][0] != 60000 || snap.Bids[0][1] != 1.5 {
		t.Errorf("bids: %v", snap.Bids)
	}
	if len(snap.Asks) != 2 {
		t.Errorf("asks: %v", snap.Asks)
	}
}

func TestParse_NumericPriceAccepted(t *testing.T) {
	a := newTestFutures()
	// Mixed type: price as number (rather than string) — parseSide should handle
	frame := []byte(`{"type":"message","topic":"/contractMarket/level2Depth50:ETHUSDTM","subject":"level2","data":{"bids":[[3000,5]],"asks":[]}}`)
	snap, _ := a.Parse(frame)
	if snap == nil || len(snap.Bids) != 1 || snap.Bids[0][0] != 3000 {
		t.Errorf("numeric price: %+v", snap)
	}
}

func TestParse_NonMessageTypeIgnored(t *testing.T) {
	a := newTestFutures()
	got, _ := a.Parse([]byte(`{"type":"welcome","id":"x"}`))
	if got != nil {
		t.Errorf("welcome should produce nil, got %+v", got)
	}
}

func TestParse_NonDepthTopicIgnored(t *testing.T) {
	a := newTestFutures()
	got, _ := a.Parse([]byte(`{"type":"message","topic":"/contractMarket/execution:XBTUSDTM","data":{}}`))
	if got != nil {
		t.Errorf("non-depth topic should produce nil, got %+v", got)
	}
}

func TestParse_NonUSDTMContractIgnored(t *testing.T) {
	a := newTestFutures()
	got, _ := a.Parse([]byte(`{"type":"message","topic":"/contractMarket/level2Depth50:BTCUSDC","data":{"bids":[],"asks":[]}}`))
	if got != nil {
		t.Errorf("non-USDTM contract should produce nil, got %+v", got)
	}
}

func TestParse_ZeroSizeFiltered(t *testing.T) {
	a := newTestFutures()
	frame := []byte(`{"type":"message","topic":"/contractMarket/level2Depth50:SOLUSDTM","subject":"level2","data":{"bids":[["150",0],["149.5",5]],"asks":[]}}`)
	snap, _ := a.Parse(frame)
	if snap == nil || len(snap.Bids) != 1 || snap.Bids[0][0] != 149.5 {
		t.Errorf("zero-size row should be filtered: %+v", snap)
	}
}
