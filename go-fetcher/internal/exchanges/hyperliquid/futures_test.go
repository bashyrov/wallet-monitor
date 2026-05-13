package hyperliquid

import (
	"testing"
)

func TestParse_L2BookFullSnapshot(t *testing.T) {
	a := &Futures{}
	// HL levels are objects {px, sz, n} — NOT arrays. levels[0] = bids, levels[1] = asks.
	frame := []byte(`{"channel":"l2Book","data":{"coin":"BTC","time":1718000001000,"levels":[
		[{"px":"60000","sz":"1.5","n":3},{"px":"59999","sz":"2.0","n":5}],
		[{"px":"60100","sz":"2.0","n":4},{"px":"60101","sz":"1.0","n":2}]
	]}}`)
	snap, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if snap == nil || snap.Symbol != "BTC" {
		t.Fatalf("snap: %+v", snap)
	}
	if len(snap.Bids) != 2 {
		t.Fatalf("bids: want 2 got %d", len(snap.Bids))
	}
	if snap.Bids[0][0] != 60000 || snap.Bids[0][1] != 1.5 {
		t.Errorf("bid0: %v", snap.Bids[0])
	}
	if len(snap.Asks) != 2 || snap.Asks[0][0] != 60100 {
		t.Errorf("asks: %v", snap.Asks)
	}
}

func TestParse_CoinUpperCased(t *testing.T) {
	a := &Futures{}
	frame := []byte(`{"channel":"l2Book","data":{"coin":"btc","levels":[[{"px":"60000","sz":"1","n":1}],[]]}}`)
	snap, _ := a.Parse(frame)
	if snap == nil || snap.Symbol != "BTC" {
		t.Errorf("symbol must uppercase, got %+v", snap)
	}
}

func TestParse_NonL2BookChannelIgnored(t *testing.T) {
	a := &Futures{}
	got, _ := a.Parse([]byte(`{"channel":"trades","data":[{"coin":"BTC"}]}`))
	if got != nil {
		t.Errorf("trades channel should produce nil, got %+v", got)
	}
}

func TestParse_ZeroSizeLevelFiltered(t *testing.T) {
	a := &Futures{}
	frame := []byte(`{"channel":"l2Book","data":{"coin":"BTC","levels":[[{"px":"60000","sz":"0","n":0},{"px":"59999","sz":"1","n":1}],[]]}}`)
	snap, _ := a.Parse(frame)
	if snap == nil {
		t.Fatal("nil snapshot")
	}
	if len(snap.Bids) != 1 {
		t.Errorf("zero-size level should be filtered, got %d bids: %v", len(snap.Bids), snap.Bids)
	}
	if snap.Bids[0][0] != 59999 {
		t.Errorf("wrong remaining bid: %v", snap.Bids[0])
	}
}

func TestParse_EmptySidesAllowed(t *testing.T) {
	a := &Futures{}
	// One-sided book — happens during low-liquidity moments
	frame := []byte(`{"channel":"l2Book","data":{"coin":"NEWCOIN","levels":[[{"px":"1","sz":"5","n":1}],[]]}}`)
	snap, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if snap == nil || snap.Symbol != "NEWCOIN" {
		t.Fatalf("snap: %+v", snap)
	}
	if len(snap.Bids) != 1 || len(snap.Asks) != 0 {
		t.Errorf("expected 1 bid 0 asks, got %d/%d", len(snap.Bids), len(snap.Asks))
	}
}
