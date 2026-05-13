package extended

import (
	"testing"
)

func newTestFutures() *Futures {
	return &Futures{
		books:   make(map[string]*book),
		lastSeq: make(map[string]int64),
	}
}

// ── SNAPSHOT seeds book + sets seq + EventTime ──────────────────────

func TestParse_SnapshotSeeds(t *testing.T) {
	a := newTestFutures()
	frame := []byte(`{"ts":1700000000000,"seq":100,"data":{"m":"BTC-USD","type":"SNAPSHOT","b":[["60000","1"]],"a":[["60100","2"]]}}`)
	snap, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if snap == nil || snap.Symbol != "BTC" {
		t.Fatalf("snap: %+v", snap)
	}
	if a.lastSeq["BTC"] != 100 {
		t.Errorf("seq not tracked")
	}
	if snap.EventTime.IsZero() {
		t.Errorf("ts should populate EventTime")
	}
	if a.books["BTC"].bids[60000] != 1 || a.books["BTC"].asks[60100] != 2 {
		t.Errorf("book not seeded: %+v", a.books["BTC"])
	}
}

// ── DELTA contiguous applies ────────────────────────────────────────

func TestParse_DeltaContiguousApplies(t *testing.T) {
	a := newTestFutures()
	_, _ = a.Parse([]byte(`{"ts":1,"seq":100,"data":{"m":"BTC-USD","type":"SNAPSHOT","b":[["60000","1"]],"a":[]}}`))
	snap, _ := a.Parse([]byte(`{"ts":2,"seq":101,"data":{"m":"BTC-USD","type":"DELTA","b":[["60000","5"]],"a":[]}}`))
	if snap == nil || snap.Bids[0][1] != 5 {
		t.Errorf("contiguous delta should update bid size to 5, got %+v", snap)
	}
	if a.lastSeq["BTC"] != 101 {
		t.Errorf("seq not advanced")
	}
}

// ── Gap drops state ─────────────────────────────────────────────────

func TestParse_GapDropsState(t *testing.T) {
	a := newTestFutures()
	_, _ = a.Parse([]byte(`{"ts":1,"seq":100,"data":{"m":"BTC-USD","type":"SNAPSHOT","b":[["60000","1"]],"a":[]}}`))
	// gap: seq=105 not 101
	snap, _ := a.Parse([]byte(`{"ts":2,"seq":105,"data":{"m":"BTC-USD","type":"DELTA","b":[["60000","5"]],"a":[]}}`))
	if snap != nil {
		t.Errorf("gap delta must NOT emit, got %+v", snap)
	}
	if _, ok := a.books["BTC"]; ok {
		t.Errorf("gap must drop book state")
	}
	if _, ok := a.lastSeq["BTC"]; ok {
		t.Errorf("gap must clear lastSeq")
	}
}

// ── SNAPSHOT after gap reseeds ──────────────────────────────────────

func TestParse_SnapshotAfterGapReseeds(t *testing.T) {
	a := newTestFutures()
	_, _ = a.Parse([]byte(`{"ts":1,"seq":100,"data":{"m":"BTC-USD","type":"SNAPSHOT","b":[["60000","1"]],"a":[]}}`))
	_, _ = a.Parse([]byte(`{"ts":2,"seq":999,"data":{"m":"BTC-USD","type":"DELTA","b":[["60000","9"]],"a":[]}}`)) // gap
	// Now a fresh snapshot — must reseed and emit.
	snap, _ := a.Parse([]byte(`{"ts":3,"seq":2000,"data":{"m":"BTC-USD","type":"SNAPSHOT","b":[["59000","7"]],"a":[]}}`))
	if snap == nil || snap.Bids[0][0] != 59000 {
		t.Errorf("snapshot must reseed after gap, got %+v", snap)
	}
}

// ── Non-USD market ignored ──────────────────────────────────────────

func TestParse_NonUSDIgnored(t *testing.T) {
	a := newTestFutures()
	got, _ := a.Parse([]byte(`{"ts":1,"seq":1,"data":{"m":"BTC-EUR","type":"SNAPSHOT","b":[],"a":[]}}`))
	if got != nil {
		t.Errorf("non-USD must be ignored")
	}
}

// ── OnReconnect clears state ────────────────────────────────────────

func TestOnReconnect_ClearsAll(t *testing.T) {
	a := newTestFutures()
	a.books["BTC"] = &book{bids: map[float64]float64{60000: 1}, asks: map[float64]float64{60100: 2}}
	a.lastSeq["BTC"] = 100
	a.OnReconnect()
	if len(a.books) != 0 || len(a.lastSeq) != 0 {
		t.Errorf("OnReconnect must clear books + lastSeq")
	}
}

// ── Size 0 deletes ──────────────────────────────────────────────────

func TestParse_SizeZeroDeletes(t *testing.T) {
	a := newTestFutures()
	_, _ = a.Parse([]byte(`{"ts":1,"seq":100,"data":{"m":"BTC-USD","type":"SNAPSHOT","b":[["60000","1"]],"a":[["60100","2"]]}}`))
	_, _ = a.Parse([]byte(`{"ts":2,"seq":101,"data":{"m":"BTC-USD","type":"DELTA","b":[["60000","0"]],"a":[]}}`))
	if _, ok := a.books["BTC"].bids[60000]; ok {
		t.Errorf("size=0 must delete bid")
	}
}
