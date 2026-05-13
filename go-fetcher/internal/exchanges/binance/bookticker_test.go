package binance

import (
	"strings"
	"testing"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

func newTestFuturesBBO() *Futures {
	return &Futures{
		filter: NewFuturesTradingFilter(),
		books:  make(map[string]*book),
		bbo:    make(map[string]*bboLevel),
	}
}

// ── URL + BuildSubscribe ───────────────────────────────────────────────

// HOTFIX 2026-05-13: bookTicker dropped from URL+SUBSCRIBE because
// the combined-stream URL with 2 streams per symbol pushed Binance
// past its 1008 policy-violation threshold for prod prewarm sizes.
// URL carries @depth20 only; parser keeps the bookTicker route in
// case the stream is ever re-added.

func TestURL_DepthOnly(t *testing.T) {
	a := newTestFuturesBBO()
	a.syms = []string{"BTC", "ETH"}
	u, _ := a.URL(nil)
	for _, want := range []string{
		"btcusdt@depth20@100ms", "ethusdt@depth20@100ms",
	} {
		if !strings.Contains(u, want) {
			t.Errorf("URL missing %q: %s", want, u)
		}
	}
	if strings.Contains(u, "@bookTicker") {
		t.Errorf("URL must NOT include @bookTicker post-hotfix: %s", u)
	}
}

func TestURL_FallbackBTCWhenNoSymbols(t *testing.T) {
	a := newTestFuturesBBO()
	u, _ := a.URL(nil)
	if !strings.Contains(u, "btcusdt@depth20") {
		t.Errorf("fallback URL must include btcusdt@depth20: %s", u)
	}
	if strings.Contains(u, "@bookTicker") {
		t.Errorf("fallback URL must NOT include @bookTicker post-hotfix: %s", u)
	}
}

// ── Parse routing ──────────────────────────────────────────────────────

// NOTE: parseDepth + parseBookTicker call a.filter.IsTrading() which
// hits a REST endpoint on cold start. tradingFilter returns true while
// uninitialised (fail-open per the package; see trading_filter.go) — so
// these tests rely on that fail-open behavior. If the filter ever
// changes to fail-closed by default, these tests will fail and the test
// helper needs a permissive stub.

func TestParse_DepthFramePopulatesDepthState(t *testing.T) {
	a := newTestFuturesBBO()
	frame := []byte(`{"stream":"btcusdt@depth20@100ms","data":{"e":"depthUpdate","s":"BTCUSDT","b":[["60000","1.5"],["59999","2.0"]],"a":[["60100","2.0"]]}}`)
	snap, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if snap == nil || snap.Symbol != "BTC" {
		t.Fatalf("snap: %+v", snap)
	}
	if a.books["BTC"] == nil || a.books["BTC"].bids[60000] != 1.5 {
		t.Errorf("depth state not seeded")
	}
	if a.bbo["BTC"] != nil && (a.bbo["BTC"].bidPx != 0) {
		t.Errorf("depth frame should not touch BBO state")
	}
}

func TestParse_BookTickerFramePopulatesBBOState(t *testing.T) {
	a := newTestFuturesBBO()
	frame := []byte(`{"stream":"btcusdt@bookTicker","data":{"e":"bookTicker","u":1,"s":"BTCUSDT","b":"60050","B":"5","a":"60055","A":"3"}}`)
	snap, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if snap == nil {
		t.Fatal("bookTicker frame must produce snapshot")
	}
	if a.bbo["BTC"] == nil || a.bbo["BTC"].bidPx != 60050 || a.bbo["BTC"].askPx != 60055 {
		t.Errorf("BBO state not seeded: %+v", a.bbo["BTC"])
	}
	if a.books["BTC"] != nil && (len(a.books["BTC"].bids) > 0) {
		t.Errorf("bookTicker frame should not touch depth state")
	}
}

func TestMergedSnapshot_BBOSplicesOverDepth(t *testing.T) {
	a := newTestFuturesBBO()
	_, _ = a.Parse([]byte(`{"stream":"btcusdt@depth20@100ms","data":{"e":"depthUpdate","s":"BTCUSDT","b":[["60000","1"]],"a":[["60100","2"]]}}`))
	snap, _ := a.Parse([]byte(`{"stream":"btcusdt@bookTicker","data":{"e":"bookTicker","u":1,"s":"BTCUSDT","b":"60050","B":"5","a":"60055","A":"3"}}`))
	if snap.Bids[0][0] != 60050 {
		t.Errorf("BBO bid should be top: %v", snap.Bids)
	}
	if snap.Asks[0][0] != 60055 {
		t.Errorf("BBO ask should be top: %v", snap.Asks)
	}
}

func TestParse_DepthReplacesNotMerges(t *testing.T) {
	a := newTestFuturesBBO()
	// Initial depth20: prices 60000, 59999
	_, _ = a.Parse([]byte(`{"stream":"btcusdt@depth20@100ms","data":{"e":"depthUpdate","s":"BTCUSDT","b":[["60000","1"],["59999","2"]],"a":[]}}`))
	// Second depth20 with completely different prices — old levels must be GONE
	_, _ = a.Parse([]byte(`{"stream":"btcusdt@depth20@100ms","data":{"e":"depthUpdate","s":"BTCUSDT","b":[["59500","5"]],"a":[]}}`))
	bk := a.books["BTC"]
	if _, ok := bk.bids[60000]; ok {
		t.Errorf("depth20 must full-replace; 60000 should be gone")
	}
	if bk.bids[59500] != 5 {
		t.Errorf("new depth not seeded")
	}
}

// ── Splice helpers ────────────────────────────────────────────────────

func TestSpliceBBOBid_AllCases(t *testing.T) {
	depth := []ws.Level{{100, 1}}
	if got := spliceBBOBid(depth, 100.5, 5); got[0][0] != 100.5 {
		t.Errorf("better prepends")
	}
	if got := spliceBBOBid(depth, 100, 7); got[0][1] != 7 {
		t.Errorf("same refreshes size")
	}
	if got := spliceBBOBid(depth, 99, 99); got[0][0] != 100 {
		t.Errorf("worse no-op")
	}
	if got := spliceBBOBid(nil, 100, 5); len(got) != 1 || got[0][0] != 100 {
		t.Errorf("empty depth seeds")
	}
}

func TestSpliceBBOAsk_AllCases(t *testing.T) {
	depth := []ws.Level{{100, 1}}
	if got := spliceBBOAsk(depth, 99.5, 5); got[0][0] != 99.5 {
		t.Errorf("lower prepends")
	}
	if got := spliceBBOAsk(depth, 100, 7); got[0][1] != 7 {
		t.Errorf("same refreshes")
	}
	if got := spliceBBOAsk(depth, 100.5, 99); got[0][0] != 100 {
		t.Errorf("worse no-op")
	}
}

// ── Misc ──────────────────────────────────────────────────────────────

func TestParse_SubscribeAckIgnored(t *testing.T) {
	a := newTestFuturesBBO()
	got, _ := a.Parse([]byte(`{"result":null,"id":1}`))
	if got != nil {
		t.Errorf("ack → nil, got %+v", got)
	}
}

func TestOnReconnect_ClearsBothStores(t *testing.T) {
	a := newTestFuturesBBO()
	a.books["BTC"] = &book{bids: map[float64]float64{60000: 1}, asks: map[float64]float64{60100: 1}}
	a.bbo["BTC"] = &bboLevel{bidPx: 60050, bidSz: 1, askPx: 60055, askSz: 1}
	a.OnReconnect()
	if len(a.books) != 0 || len(a.bbo) != 0 {
		t.Errorf("OnReconnect must clear both")
	}
}
