package extended

import (
	"testing"
)

func newTestFutures() *Futures {
	return &Futures{
		books: make(map[string]*book),
	}
}

// Wire format verified live 2026-05-13 via WS probe:
//
//	{"type":"SNAPSHOT","data":{"t":"SNAPSHOT","m":"BTC-USD",
//	 "b":[{"q":"0.00071","p":"79125"},...],
//	 "a":[{"q":"0.36975","p":"79126"},...]}, "ts":1778692308650}
//
// All test fixtures here mirror that shape.

func TestParse_SnapshotSeeds(t *testing.T) {
	a := newTestFutures()
	frame := []byte(`{"type":"SNAPSHOT","ts":1700000000000,"seq":100,"data":{"t":"SNAPSHOT","m":"BTC-USD","b":[{"q":"1","p":"60000"}],"a":[{"q":"2","p":"60100"}]}}`)
	snap, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if snap == nil || snap.Symbol != "BTC" {
		t.Fatalf("snap: %+v", snap)
	}
	if snap.EventTime.IsZero() {
		t.Errorf("ts should populate EventTime")
	}
	if a.books["BTC"].bids[60000] != 1 || a.books["BTC"].asks[60100] != 2 {
		t.Errorf("book not seeded: %+v", a.books["BTC"])
	}
}

func TestParse_DeltaApplies(t *testing.T) {
	a := newTestFutures()
	_, _ = a.Parse([]byte(`{"type":"SNAPSHOT","ts":1,"seq":100,"data":{"t":"SNAPSHOT","m":"BTC-USD","b":[{"q":"1","p":"60000"}],"a":[]}}`))
	// seq gap is no longer enforced — all deltas apply regardless of seq jump
	snap, _ := a.Parse([]byte(`{"type":"DELTA","ts":2,"seq":999,"data":{"t":"DELTA","m":"BTC-USD","b":[{"q":"5","p":"60000"}],"a":[]}}`))
	if snap == nil || snap.Bids[0][1] != 5 {
		t.Errorf("delta should update bid size to 5, got %+v", snap)
	}
}

func TestParse_SnapshotReseeds(t *testing.T) {
	a := newTestFutures()
	_, _ = a.Parse([]byte(`{"type":"SNAPSHOT","ts":1,"seq":100,"data":{"t":"SNAPSHOT","m":"BTC-USD","b":[{"q":"1","p":"60000"}],"a":[]}}`))
	_, _ = a.Parse([]byte(`{"type":"DELTA","ts":2,"seq":999,"data":{"t":"DELTA","m":"BTC-USD","b":[{"q":"9","p":"60000"}],"a":[]}}`))
	snap, _ := a.Parse([]byte(`{"type":"SNAPSHOT","ts":3,"seq":2000,"data":{"t":"SNAPSHOT","m":"BTC-USD","b":[{"q":"7","p":"59000"}],"a":[]}}`))
	if snap == nil || snap.Bids[0][0] != 59000 {
		t.Errorf("snapshot must reseed, got %+v", snap)
	}
}

func TestParse_NonUSDIgnored(t *testing.T) {
	a := newTestFutures()
	got, _ := a.Parse([]byte(`{"type":"SNAPSHOT","ts":1,"seq":1,"data":{"t":"SNAPSHOT","m":"BTC-EUR","b":[],"a":[]}}`))
	if got != nil {
		t.Errorf("non-USD must be ignored")
	}
}

func TestOnReconnect_ClearsAll(t *testing.T) {
	a := newTestFutures()
	a.books["BTC"] = &book{bids: map[float64]float64{60000: 1}, asks: map[float64]float64{60100: 2}}
	a.OnReconnect()
	if len(a.books) != 0 {
		t.Errorf("OnReconnect must clear books, got %d entries", len(a.books))
	}
}

func TestParse_SizeZeroDeletes(t *testing.T) {
	a := newTestFutures()
	_, _ = a.Parse([]byte(`{"type":"SNAPSHOT","ts":1,"seq":100,"data":{"t":"SNAPSHOT","m":"BTC-USD","b":[{"q":"1","p":"60000"}],"a":[{"q":"2","p":"60100"}]}}`))
	_, _ = a.Parse([]byte(`{"type":"DELTA","ts":2,"seq":101,"data":{"t":"DELTA","m":"BTC-USD","b":[{"q":"0","p":"60000"}],"a":[]}}`))
	if _, ok := a.books["BTC"].bids[60000]; ok {
		t.Errorf("size=0 must delete bid")
	}
}

func TestParse_DataTypeFallback(t *testing.T) {
	a := newTestFutures()
	frame := []byte(`{"ts":1,"seq":100,"data":{"t":"SNAPSHOT","m":"BTC-USD","b":[{"q":"1","p":"60000"}],"a":[]}}`)
	snap, _ := a.Parse(frame)
	if snap == nil || snap.Bids[0][0] != 60000 {
		t.Errorf("data.t fallback should work, got %+v", snap)
	}
}

func TestParse_UnknownTypeIgnored(t *testing.T) {
	a := newTestFutures()
	frame := []byte(`{"type":"OTHER","ts":1,"seq":100,"data":{"t":"OTHER","m":"BTC-USD","b":[],"a":[]}}`)
	got, _ := a.Parse(frame)
	if got != nil {
		t.Errorf("unknown type must be ignored")
	}
}
