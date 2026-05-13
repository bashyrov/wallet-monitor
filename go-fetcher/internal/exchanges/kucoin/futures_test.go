package kucoin

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func newTestFutures() *Futures {
	return &Futures{auth: &authClient{}, books: make(map[string]*book)}
}

// ── Subscribe shape ───────────────────────────────────────────────────

func TestBuildSubscribe_TargetsRawLevel2(t *testing.T) {
	a := newTestFutures()
	// async REST seed → dead port, hermetic.
	saved := restSnapshot
	restSnapshot = "http://127.0.0.1:1/?symbol=%s"
	defer func() { restSnapshot = saved }()

	frames := a.BuildSubscribe([]string{"BTC", "ETH"})
	if len(frames) != 2 {
		t.Fatalf("expected 2 frames, got %d", len(frames))
	}
	if !strings.Contains(string(frames[0]), "/contractMarket/level2:XBTUSDTM") {
		t.Errorf("BTC → XBT alias missing: %s", frames[0])
	}
	if !strings.Contains(string(frames[1]), "/contractMarket/level2:ETHUSDTM") {
		t.Errorf("ETH topic missing: %s", frames[1])
	}
	if strings.Contains(string(frames[0]), "level2Depth50") {
		t.Errorf("should NOT subscribe to legacy level2Depth50 channel")
	}
}

// ── BTC ↔ XBT aliasing ────────────────────────────────────────────────

func TestTokenContractAliasing(t *testing.T) {
	if tokenToContract("BTC") != "XBTUSDTM" {
		t.Errorf("BTC → XBTUSDTM, got %s", tokenToContract("BTC"))
	}
	if tokenToContract("ETH") != "ETHUSDTM" {
		t.Errorf("ETH → ETHUSDTM, got %s", tokenToContract("ETH"))
	}
	if contractToToken("XBTUSDTM") != "BTC" {
		t.Errorf("XBTUSDTM → BTC")
	}
	if contractToToken("ETHUSDTM") != "ETH" {
		t.Errorf("ETHUSDTM → ETH")
	}
}

// ── Pre-snapshot buffering ────────────────────────────────────────────

func TestParse_DeltaBeforeSnapshotIsBuffered(t *testing.T) {
	a := newTestFutures()
	frame := []byte(`{"type":"message","topic":"/contractMarket/level2:XBTUSDTM","subject":"level2","data":{"sequence":100,"change":"60000,buy,1","timestamp":1}}`)
	got, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if got != nil {
		t.Errorf("pre-snapshot delta should not emit, got %+v", got)
	}
	bk := a.books["BTC"]
	if bk == nil || len(bk.buffer) != 1 {
		t.Errorf("delta should be buffered: %+v", bk)
	}
}

// ── REST seed + buffered drain happy path ────────────────────────────

func TestSeedREST_DrainsBufferContiguous(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		// snapshot sequence=100; bid 60000@1, ask 60100@2 (strings — matches KuCoin REST)
		fmt.Fprint(w, `{"data":{"sequence":"100","bids":[["60000","1"]],"asks":[["60100","2"]]}}`)
	}))
	defer srv.Close()
	saved := restSnapshot
	restSnapshot = srv.URL + "/?symbol=%s"
	defer func() { restSnapshot = saved }()

	a := newTestFutures()
	// Buffer the strict-next delta (seq=101 = baseSeq+1)
	_, _ = a.Parse([]byte(`{"type":"message","topic":"/contractMarket/level2:XBTUSDTM","subject":"level2","data":{"sequence":101,"change":"60000,buy,3","timestamp":1}}`))
	a.seedREST("BTC")

	bk := a.books["BTC"]
	if bk.baseSeq != 100 || !bk.seeded {
		t.Fatalf("expected seeded with baseSeq=100, got baseSeq=%d seeded=%v", bk.baseSeq, bk.seeded)
	}
	if bk.lastSeq != 101 {
		t.Errorf("lastSeq should be 101, got %d", bk.lastSeq)
	}
	if bk.bids[60000] != 3 {
		t.Errorf("bid 60000 should be 3 (delta overrides snapshot), got %v", bk.bids[60000])
	}
	if bk.asks[60100] != 2 {
		t.Errorf("ask 60100 from snapshot should persist (no delta), got %v", bk.asks[60100])
	}
}

// ── Stale buffered events dropped ────────────────────────────────────

func TestSeedREST_DropsStaleBufferedEvents(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		fmt.Fprint(w, `{"data":{"sequence":"200","bids":[["60000","1"]],"asks":[]}}`)
	}))
	defer srv.Close()
	saved := restSnapshot
	restSnapshot = srv.URL + "/?symbol=%s"
	defer func() { restSnapshot = saved }()

	a := newTestFutures()
	// Stale (seq=50 < baseSeq=200) — must be dropped.
	_, _ = a.Parse([]byte(`{"type":"message","topic":"/contractMarket/level2:XBTUSDTM","subject":"level2","data":{"sequence":50,"change":"99999,buy,99","timestamp":1}}`))
	// Strict-next (seq=201 = baseSeq+1) — must apply.
	_, _ = a.Parse([]byte(`{"type":"message","topic":"/contractMarket/level2:XBTUSDTM","subject":"level2","data":{"sequence":201,"change":"60000,buy,7","timestamp":1}}`))
	a.seedREST("BTC")

	bk := a.books["BTC"]
	if _, ok := bk.bids[99999]; ok {
		t.Errorf("stale bid 99999 must not appear")
	}
	if bk.bids[60000] != 7 {
		t.Errorf("valid delta should apply, got bid 60000 = %v", bk.bids[60000])
	}
	if bk.lastSeq != 201 {
		t.Errorf("lastSeq should be 201, got %d", bk.lastSeq)
	}
}

// ── Bootstrap-edge gap ──────────────────────────────────────────────

func TestSeedREST_GapAtBootstrapResetsBaseSeq(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		fmt.Fprint(w, `{"data":{"sequence":"500","bids":[],"asks":[]}}`)
	}))
	defer srv.Close()
	saved := restSnapshot
	restSnapshot = srv.URL + "/?symbol=%s"
	defer func() { restSnapshot = saved }()

	a := newTestFutures()
	// Buffered delta seq=505 — skips 501/502/503/504 from baseSeq+1.
	_, _ = a.Parse([]byte(`{"type":"message","topic":"/contractMarket/level2:XBTUSDTM","subject":"level2","data":{"sequence":505,"change":"60000,buy,1","timestamp":1}}`))
	a.seedREST("BTC")

	bk := a.books["BTC"]
	if bk.baseSeq != 0 || bk.seeded {
		t.Errorf("gap at edge must reset baseSeq + leave un-seeded, got baseSeq=%d seeded=%v", bk.baseSeq, bk.seeded)
	}
}

// ── Steady-state gap forces resnap ───────────────────────────────────

func TestParse_GapInSteadyStateResnaps(t *testing.T) {
	a := newTestFutures()
	a.books["BTC"] = &book{
		bids:    map[float64]float64{60000: 1},
		asks:    map[float64]float64{60100: 2},
		baseSeq: 1000,
		lastSeq: 1050,
		seeded:  true,
	}
	// Contiguous (seq=1051) — should apply.
	snap, _ := a.Parse([]byte(`{"type":"message","topic":"/contractMarket/level2:XBTUSDTM","subject":"level2","data":{"sequence":1051,"change":"60000,buy,5","timestamp":1}}`))
	if snap == nil || snap.Bids[0][1] != 5 {
		t.Errorf("contiguous should emit snap with bid size 5, got %+v", snap)
	}

	// Gap (seq=1100 not 1052) — must reset.
	snap2, _ := a.Parse([]byte(`{"type":"message","topic":"/contractMarket/level2:XBTUSDTM","subject":"level2","data":{"sequence":1100,"change":"60000,buy,9","timestamp":1}}`))
	if snap2 != nil {
		t.Errorf("gap must NOT emit, got %+v", snap2)
	}
	bk := a.books["BTC"]
	if bk.seeded || bk.baseSeq != 0 {
		t.Errorf("gap should leave un-seeded with baseSeq=0, got seeded=%v baseSeq=%d", bk.seeded, bk.baseSeq)
	}
	if len(bk.bids) != 0 {
		t.Errorf("gap should clear bids")
	}
}

// ── Side semantics ──────────────────────────────────────────────────

func TestApplyChange_BuySellSeparation(t *testing.T) {
	a := newTestFutures()
	a.books["BTC"] = &book{
		bids:    map[float64]float64{60000: 1},
		asks:    map[float64]float64{60100: 2},
		baseSeq: 1, lastSeq: 10, seeded: true,
	}
	// buy delta hits bids only.
	_, _ = a.Parse([]byte(`{"type":"message","topic":"/contractMarket/level2:XBTUSDTM","subject":"level2","data":{"sequence":11,"change":"60000,buy,5","timestamp":1}}`))
	if a.books["BTC"].bids[60000] != 5 {
		t.Errorf("buy delta should hit bid")
	}
	if a.books["BTC"].asks[60100] != 2 {
		t.Errorf("buy delta must not touch ask")
	}
	// sell delta hits asks only with delete on size=0.
	_, _ = a.Parse([]byte(`{"type":"message","topic":"/contractMarket/level2:XBTUSDTM","subject":"level2","data":{"sequence":12,"change":"60100,sell,0","timestamp":1}}`))
	if _, ok := a.books["BTC"].asks[60100]; ok {
		t.Errorf("sell size=0 should remove ask")
	}
	if a.books["BTC"].bids[60000] != 5 {
		t.Errorf("sell delta must not touch bid")
	}
}

// ── Wrong topic + wrong type + malformed change ─────────────────────

func TestParse_WrongTopicIgnored(t *testing.T) {
	a := newTestFutures()
	got, _ := a.Parse([]byte(`{"type":"message","topic":"/contractMarket/ticker:XBTUSDTM","subject":"ticker","data":{"sequence":1,"change":"60000,buy,1"}}`))
	if got != nil {
		t.Errorf("wrong topic must be ignored")
	}
}

func TestParse_PingPongIgnored(t *testing.T) {
	a := newTestFutures()
	got, _ := a.Parse([]byte(`{"id":"1","type":"pong"}`))
	if got != nil {
		t.Errorf("pong must be ignored")
	}
}

func TestParse_MalformedChangeIgnored(t *testing.T) {
	a := newTestFutures()
	// only 2 fields
	got, _ := a.Parse([]byte(`{"type":"message","topic":"/contractMarket/level2:XBTUSDTM","subject":"level2","data":{"sequence":1,"change":"60000,buy","timestamp":1}}`))
	if got != nil {
		t.Errorf("malformed change must be ignored")
	}
}

// ── OnReconnect clears state ────────────────────────────────────────

func TestOnReconnect_ClearsBooks(t *testing.T) {
	a := newTestFutures()
	a.books["BTC"] = &book{
		bids: map[float64]float64{60000: 1}, asks: map[float64]float64{60100: 2},
		baseSeq: 100, lastSeq: 200, seeded: true,
	}
	a.OnReconnect()
	if len(a.books) != 0 {
		t.Errorf("OnReconnect must clear books, got %d entries", len(a.books))
	}
}

// ── REST seed error handling ────────────────────────────────────────

func TestSeedREST_HTTPErrorNoOp(t *testing.T) {
	saved := restSnapshot
	restSnapshot = "http://127.0.0.1:1/?symbol=%s"
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
		t.Fatal("seedREST hung on dial error")
	}
	if bk := a.books["BTC"]; bk != nil && bk.baseSeq != 0 {
		t.Errorf("failed seed must not set baseSeq")
	}
}

// ── anyNum decoder accepts both number + string ─────────────────────

func TestAnyNum_AcceptsBothEncodings(t *testing.T) {
	var n1 anyNum
	if err := n1.UnmarshalJSON([]byte(`123`)); err != nil || n1.Uint64() != 123 {
		t.Errorf("number form failed: %v %d", err, n1.Uint64())
	}
	var n2 anyNum
	if err := n2.UnmarshalJSON([]byte(`"456"`)); err != nil || n2.Uint64() != 456 {
		t.Errorf("string form failed: %v %d", err, n2.Uint64())
	}
	var n3 anyNum
	if err := n3.UnmarshalJSON([]byte(`"not-a-number"`)); err != nil || n3.Uint64() != 0 {
		t.Errorf("bad string must soft-fail to zero, got err=%v v=%d", err, n3.Uint64())
	}
}
