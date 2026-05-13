package gate

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func newTestFutures() *Futures {
	return &Futures{books: make(map[string]*book)}
}

// ── Subscribe ─────────────────────────────────────────────────────────

func TestBuildSubscribe_EmitsOrderBookUpdate(t *testing.T) {
	a := newTestFutures()
	// async REST seed will fire; redirect to a noop server so tests stay hermetic.
	saved := restSnapshot
	restSnapshot = "http://127.0.0.1:1/%s"
	defer func() { restSnapshot = saved }()

	frames := a.BuildSubscribe([]string{"BTC", "ETH"})
	if len(frames) != 2 {
		t.Fatalf("expected 2 frames, got %d", len(frames))
	}
	for _, f := range frames {
		s := string(f)
		if !strings.Contains(s, "futures.order_book_update") {
			t.Errorf("frame must reference order_book_update channel: %s", s)
		}
		if !strings.Contains(s, "100ms") || !strings.Contains(s, `"20"`) {
			t.Errorf("frame must request 100ms / lvl=20: %s", s)
		}
	}
}

// ── Parse: subscribe ack ignored ─────────────────────────────────────

func TestParse_SubscribeAckIgnored(t *testing.T) {
	a := newTestFutures()
	frame := []byte(`{"channel":"futures.order_book_update","event":"subscribe","result":{"status":"success"}}`)
	got, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if got != nil {
		t.Errorf("ack must return nil snapshot, got %+v", got)
	}
}

// ── Pre-snapshot buffering ───────────────────────────────────────────

func TestParse_DeltaBeforeSnapshotIsBuffered(t *testing.T) {
	a := newTestFutures()
	frame := []byte(`{"channel":"futures.order_book_update","event":"update","result":{"s":"BTC_USDT","U":100,"u":105,"b":[{"p":"60000","s":1}],"a":[]}}`)
	got, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if got != nil {
		t.Errorf("pre-snapshot delta should not emit snapshot, got %+v", got)
	}
	bk := a.books["BTC"]
	if bk == nil || len(bk.buffer) != 1 {
		t.Errorf("delta should be buffered: %+v", bk)
	}
}

// ── REST snapshot + buffered drain happy path ────────────────────────

func TestSeedREST_DrainsBufferContiguous(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// snapshot id 100; bid 60000@1, ask 60100@2
		fmt.Fprint(w, `{"id":100,"bids":[{"p":"60000","s":1}],"asks":[{"p":"60100","s":2}]}`)
	}))
	defer srv.Close()
	saved := restSnapshot
	restSnapshot = srv.URL + "/?contract=%s&limit=20&with_id=true"
	defer func() { restSnapshot = saved }()

	a := newTestFutures()
	// Buffer a delta that straddles baseID+1=101 (U=99, u=102, includes 101)
	_, _ = a.Parse([]byte(`{"channel":"futures.order_book_update","event":"update","result":{"s":"BTC_USDT","U":99,"u":102,"b":[{"p":"60000","s":3}],"a":[{"p":"60100","s":0}]}}`))
	a.seedREST("BTC")

	bk := a.books["BTC"]
	if bk.baseID != 100 || !bk.seeded {
		t.Fatalf("expected seeded with baseID=100, got baseID=%d seeded=%v", bk.baseID, bk.seeded)
	}
	if bk.lastU != 102 {
		t.Errorf("lastU should advance to 102, got %d", bk.lastU)
	}
	if bk.bids[60000] != 3 {
		t.Errorf("bid 60000 should be 3 (delta overrides snapshot), got %v", bk.bids[60000])
	}
	if _, ok := bk.asks[60100]; ok {
		t.Errorf("ask 60100 should be removed by size=0 delta")
	}
	if len(bk.buffer) != 0 {
		t.Errorf("buffer should be empty after drain")
	}
}

// ── Stale events before baseID dropped ───────────────────────────────

func TestSeedREST_DropsStaleBufferedEvents(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		fmt.Fprint(w, `{"id":200,"bids":[{"p":"60000","s":1}],"asks":[]}`)
	}))
	defer srv.Close()
	saved := restSnapshot
	restSnapshot = srv.URL + "/?contract=%s&limit=20&with_id=true"
	defer func() { restSnapshot = saved }()

	a := newTestFutures()
	// Stale delta entirely before baseID — must be discarded.
	_, _ = a.Parse([]byte(`{"channel":"futures.order_book_update","event":"update","result":{"s":"BTC_USDT","U":50,"u":80,"b":[{"p":"99999","s":99}],"a":[]}}`))
	// Valid bootstrap-edge delta (U=199, u=205, straddles 201).
	_, _ = a.Parse([]byte(`{"channel":"futures.order_book_update","event":"update","result":{"s":"BTC_USDT","U":199,"u":205,"b":[{"p":"60000","s":7}],"a":[]}}`))
	a.seedREST("BTC")

	bk := a.books["BTC"]
	if bk.bids[99999] != 0 {
		t.Errorf("stale bid 99999 should NOT have been applied: %v", bk.bids[99999])
	}
	if bk.bids[60000] != 7 {
		t.Errorf("valid edge delta should apply, got bid 60000 = %v", bk.bids[60000])
	}
	if bk.lastU != 205 {
		t.Errorf("lastU should be 205, got %d", bk.lastU)
	}
}

// ── Gap detection — steady state ─────────────────────────────────────

func TestParse_GapInSteadyStateForcesResnapshot(t *testing.T) {
	a := newTestFutures()
	// Seed inline (avoid REST):
	a.books["BTC"] = &book{
		bids:   map[float64]float64{60000: 1},
		asks:   map[float64]float64{60100: 2},
		baseID: 1000,
		lastU:  1050,
		seeded: true,
	}
	// Contiguous delta — should apply.
	snap, _ := a.Parse([]byte(`{"channel":"futures.order_book_update","event":"update","result":{"s":"BTC_USDT","U":1051,"u":1052,"b":[{"p":"60000","s":5}],"a":[]}}`))
	if snap == nil || snap.Bids[0][1] != 5 {
		t.Errorf("contiguous delta should produce snap with bid size 5, got %+v", snap)
	}

	// Gap! Expected U=1053, got U=1099.
	snap2, _ := a.Parse([]byte(`{"channel":"futures.order_book_update","event":"update","result":{"s":"BTC_USDT","U":1099,"u":1100,"b":[{"p":"60000","s":9}],"a":[]}}`))
	if snap2 != nil {
		t.Errorf("gap event must NOT emit, got %+v", snap2)
	}
	bk := a.books["BTC"]
	if bk.seeded || bk.baseID != 0 || bk.lastU != 0 {
		t.Errorf("gap must reset to un-seeded: seeded=%v baseID=%d lastU=%d", bk.seeded, bk.baseID, bk.lastU)
	}
	if len(bk.bids) != 0 {
		t.Errorf("gap should clear bids, got %v", bk.bids)
	}
}

// ── Size 0 deletes ───────────────────────────────────────────────────

func TestApplyDelta_SizeZeroRemoves(t *testing.T) {
	a := newTestFutures()
	a.books["BTC"] = &book{
		bids:   map[float64]float64{60000: 1, 59999: 2},
		asks:   map[float64]float64{60100: 3},
		baseID: 1,
		lastU:  10,
		seeded: true,
	}
	snap, _ := a.Parse([]byte(`{"channel":"futures.order_book_update","event":"update","result":{"s":"BTC_USDT","U":11,"u":12,"b":[{"p":"59999","s":0}],"a":[{"p":"60100","s":0}]}}`))
	if snap == nil {
		t.Fatal("expected snapshot")
	}
	bk := a.books["BTC"]
	if _, ok := bk.bids[59999]; ok {
		t.Error("bid 59999 should be deleted")
	}
	if _, ok := bk.asks[60100]; ok {
		t.Error("ask 60100 should be deleted")
	}
	if bk.bids[60000] != 1 {
		t.Errorf("untouched bid 60000 should remain")
	}
}

// ── Non-USDT contract ignored ────────────────────────────────────────

func TestParse_NonUSDTIgnored(t *testing.T) {
	a := newTestFutures()
	frame := []byte(`{"channel":"futures.order_book_update","event":"update","result":{"s":"BTC_USD","U":1,"u":2,"b":[{"p":"60000","s":1}],"a":[]}}`)
	got, _ := a.Parse(frame)
	if got != nil {
		t.Errorf("non-USDT must be ignored, got %+v", got)
	}
}

// ── Wrong channel ignored ────────────────────────────────────────────

func TestParse_WrongChannelIgnored(t *testing.T) {
	a := newTestFutures()
	frame := []byte(`{"channel":"futures.tickers","event":"update","result":{"s":"BTC_USDT"}}`)
	got, _ := a.Parse(frame)
	if got != nil {
		t.Errorf("wrong-channel must be ignored")
	}
}

// ── OnReconnect clears state ─────────────────────────────────────────

func TestOnReconnect_ClearsBooks(t *testing.T) {
	a := newTestFutures()
	a.books["BTC"] = &book{
		bids:   map[float64]float64{60000: 1},
		asks:   map[float64]float64{60100: 2},
		baseID: 100, lastU: 200, seeded: true,
	}
	a.OnReconnect()
	if len(a.books) != 0 {
		t.Errorf("OnReconnect must clear books, got %d entries", len(a.books))
	}
}

// ── Snapshot id zero — seed treated as no-op ─────────────────────────

func TestSeedREST_IDZeroIgnored(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		fmt.Fprint(w, `{"id":0,"bids":[],"asks":[]}`)
	}))
	defer srv.Close()
	saved := restSnapshot
	restSnapshot = srv.URL + "/?contract=%s&limit=20&with_id=true"
	defer func() { restSnapshot = saved }()

	a := newTestFutures()
	a.seedREST("BTC")
	if bk := a.books["BTC"]; bk != nil && bk.baseID != 0 {
		t.Errorf("id=0 response must not set baseID, got %d", bk.baseID)
	}
}

// ── HTTP error — seed treated as no-op ──────────────────────────────

func TestSeedREST_HTTPErrorNoOp(t *testing.T) {
	// Point to a closed port so dial fails fast.
	saved := restSnapshot
	restSnapshot = "http://127.0.0.1:1/?contract=%s&limit=20&with_id=true"
	defer func() { restSnapshot = saved }()

	a := newTestFutures()
	done := make(chan struct{})
	go func() {
		a.seedREST("BTC")
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(8 * time.Second):
		t.Fatal("seedREST hung — should bail on dial error")
	}
	if bk := a.books["BTC"]; bk != nil && bk.baseID != 0 {
		t.Errorf("failed seed must not set baseID")
	}
}
