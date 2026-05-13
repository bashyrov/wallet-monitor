package aster

import (
	"testing"
)

// Aster is a Binance fork (same partial-book stream wire format). Bare
// /ws endpoint puts e/E/T/s/b/a fields at the top level, no `data`
// wrapper — Parse falls back to parsing the whole frame in that case.
// Adapter is stateless.
func TestParse_BareWSFrameTopLevelFields(t *testing.T) {
	a := &Futures{}
	frame := []byte(`{"e":"depthUpdate","E":1718000001000,"T":1718000001000,"s":"BTCUSDT","b":[["60000","1.5"]],"a":[["60100","2.0"]]}`)
	snap, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if snap == nil || snap.Symbol != "BTC" {
		t.Fatalf("snap: %+v", snap)
	}
	if len(snap.Bids) != 1 || snap.Bids[0][0] != 60000 || snap.Bids[0][1] != 1.5 {
		t.Errorf("bids: %v", snap.Bids)
	}
}

func TestParse_CombinedStreamWrapper(t *testing.T) {
	a := &Futures{}
	// Combined-stream form (less common on Aster but should still parse)
	frame := []byte(`{"stream":"ethusdt@depth20@100ms","data":{"s":"ETHUSDT","b":[["3000","5"]],"a":[["3001","2"]]}}`)
	snap, _ := a.Parse(frame)
	if snap == nil || snap.Symbol != "ETH" {
		t.Errorf("combined-stream form: %+v", snap)
	}
}

func TestParse_SubscribeAckIgnored(t *testing.T) {
	a := &Futures{}
	got, _ := a.Parse([]byte(`{"result":null,"id":1}`))
	if got != nil {
		t.Errorf("subscribe-ack should produce nil, got %+v", got)
	}
}

func TestParse_NonUSDTSymbolIgnored(t *testing.T) {
	a := &Futures{}
	got, _ := a.Parse([]byte(`{"s":"BTCBUSD","b":[["60000","1"]],"a":[]}`))
	if got != nil {
		t.Errorf("non-USDT symbol should produce nil, got %+v", got)
	}
}

func TestParse_ZeroSizeFiltered(t *testing.T) {
	a := &Futures{}
	frame := []byte(`{"s":"SOLUSDT","b":[["150","0"],["149.5","5"]],"a":[]}`)
	snap, _ := a.Parse(frame)
	if snap == nil || len(snap.Bids) != 1 || snap.Bids[0][0] != 149.5 {
		t.Errorf("zero-size: %+v", snap)
	}
}

func TestParse_LegacyBidsAsksFallback(t *testing.T) {
	a := &Futures{}
	// Fallback path: lowercase b/a empty → use Bids/Asks
	frame := []byte(`{"s":"BTCUSDT","bids":[["60000","1"]],"asks":[["60100","2"]]}`)
	snap, _ := a.Parse(frame)
	if snap == nil || len(snap.Bids) != 1 || snap.Bids[0][0] != 60000 {
		t.Errorf("Bids/Asks fallback: %+v", snap)
	}
}
