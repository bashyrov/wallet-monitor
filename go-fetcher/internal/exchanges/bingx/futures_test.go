package bingx

import (
	"testing"
)

// BingX @depth20 pushes ~500ms server-fixed snapshots. Adapter is
// stateless — each frame produces a fresh Snapshot from frame data.
func TestParse_FullSnapshot(t *testing.T) {
	a := &Futures{}
	frame := []byte(`{"dataType":"BTC-USDT@depth20","data":{"bids":[["60000","1.5"],["59999","2.0"]],"asks":[["60100","2.0"]]}}`)
	snap, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if snap == nil || snap.Symbol != "BTC" {
		t.Fatalf("snap: %+v", snap)
	}
	if len(snap.Bids) != 2 || snap.Bids[0][0] != 60000 || snap.Bids[0][1] != 1.5 {
		t.Errorf("bids: %v", snap.Bids)
	}
	if len(snap.Asks) != 1 || snap.Asks[0][0] != 60100 {
		t.Errorf("asks: %v", snap.Asks)
	}
}

func TestParse_ZeroSizeFiltered(t *testing.T) {
	a := &Futures{}
	frame := []byte(`{"dataType":"ETH-USDT@depth20","data":{"bids":[["3000","0"],["2999","5"]],"asks":[]}}`)
	snap, _ := a.Parse(frame)
	if snap == nil || len(snap.Bids) != 1 || snap.Bids[0][0] != 2999 {
		t.Errorf("zero-size should be filtered: %+v", snap)
	}
}

func TestParse_NonDepthDataTypeIgnored(t *testing.T) {
	a := &Futures{}
	got, _ := a.Parse([]byte(`{"dataType":"BTC-USDT@trade","data":[]}`))
	if got != nil {
		t.Errorf("@trade dataType should produce nil, got %+v", got)
	}
}

func TestParse_NonUSDTPairIgnored(t *testing.T) {
	a := &Futures{}
	got, _ := a.Parse([]byte(`{"dataType":"BTC-USDC@depth20","data":{"bids":[["60000","1"]],"asks":[]}}`))
	if got != nil {
		t.Errorf("non-USDT pair should produce nil, got %+v", got)
	}
}

func TestParse_DataTypePatternMatchesAnyDepth(t *testing.T) {
	// Adapter checks "@depth" substring, not exact "@depth20" — so a
	// future @depth50 frame would also parse.
	a := &Futures{}
	frame := []byte(`{"dataType":"BTC-USDT@depth50","data":{"bids":[["60000","1"]],"asks":[]}}`)
	snap, _ := a.Parse(frame)
	if snap == nil || snap.Symbol != "BTC" {
		t.Errorf("depth50 should also parse, got %+v", snap)
	}
}
