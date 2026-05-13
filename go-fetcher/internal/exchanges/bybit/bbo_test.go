package bybit

import (
	"strings"
	"testing"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

func newTestFuturesBBO() *Futures {
	return &Futures{
		books: make(map[string]*book),
		bbo:   make(map[string]*bboLevel),
	}
}

// ── BuildSubscribe ─────────────────────────────────────────────────────

func TestBuildSubscribe_TwoFramesPerSymbol(t *testing.T) {
	a := newTestFuturesBBO()
	frames := a.BuildSubscribe([]string{"BTC", "ETH"})
	if len(frames) != 4 {
		t.Fatalf("want 4 frames (2 symbols × 2 topics) got %d", len(frames))
	}
	all := strings.Join(framesToStrings(frames), " ")
	for _, want := range []string{
		"orderbook.50.BTCUSDT", "orderbook.1.BTCUSDT",
		"orderbook.50.ETHUSDT", "orderbook.1.ETHUSDT",
	} {
		if !strings.Contains(all, want) {
			t.Errorf("missing %q in subscribe frames: %s", want, all)
		}
	}
}

// ── BBO splice helpers ─────────────────────────────────────────────────

func TestSpliceBBOBid_StrictlyBetterPrepends(t *testing.T) {
	depth := []ws.Level{{100, 1}, {99, 2}, {98, 3}}
	got := spliceBBOBid(depth, 100.5, 5) // higher than depth top
	if len(got) != 4 || got[0][0] != 100.5 || got[0][1] != 5 {
		t.Errorf("BBO better should prepend: %v", got)
	}
}

func TestSpliceBBOBid_SamePriceRefreshesSize(t *testing.T) {
	depth := []ws.Level{{100, 1}, {99, 2}}
	got := spliceBBOBid(depth, 100, 7)
	if len(got) != 2 || got[0][0] != 100 || got[0][1] != 7 {
		t.Errorf("BBO same price should refresh size: %v", got)
	}
}

func TestSpliceBBOBid_WorsePriceLeavesDepthAlone(t *testing.T) {
	depth := []ws.Level{{100, 1}, {99, 2}}
	got := spliceBBOBid(depth, 99.5, 99)
	if len(got) != 2 || got[0][0] != 100 {
		t.Errorf("BBO worse should leave depth as-is, got %v", got)
	}
}

func TestSpliceBBOBid_ZeroBBONoOp(t *testing.T) {
	depth := []ws.Level{{100, 1}}
	got := spliceBBOBid(depth, 0, 0)
	if len(got) != 1 || got[0][0] != 100 {
		t.Errorf("BBO zero px → no-op, got %v", got)
	}
}

func TestSpliceBBOBid_EmptyDepthSeedsFromBBO(t *testing.T) {
	got := spliceBBOBid(nil, 100, 5)
	if len(got) != 1 || got[0][0] != 100 || got[0][1] != 5 {
		t.Errorf("empty depth: BBO seeds top, got %v", got)
	}
}

func TestSpliceBBOAsk_BetterMeansLower(t *testing.T) {
	depth := []ws.Level{{100, 1}, {101, 2}}
	got := spliceBBOAsk(depth, 99.5, 5) // strictly lower = better
	if len(got) != 3 || got[0][0] != 99.5 {
		t.Errorf("better ask should prepend: %v", got)
	}
}

func TestSpliceBBOAsk_WorseLeavesDepth(t *testing.T) {
	depth := []ws.Level{{100, 1}, {101, 2}}
	got := spliceBBOAsk(depth, 100.5, 99)
	if len(got) != 2 || got[0][0] != 100 {
		t.Errorf("worse ask should leave depth: %v", got)
	}
}

// ── Frame routing ──────────────────────────────────────────────────────

func TestParse_RoutesOrderbook50ToDepth(t *testing.T) {
	a := newTestFuturesBBO()
	frame := []byte(`{"topic":"orderbook.50.BTCUSDT","type":"snapshot","data":{"s":"BTCUSDT","b":[["60000","1.5"]],"a":[["60100","2.0"]]}}`)
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
	// BBO state untouched
	if a.bbo["BTC"] != nil && (a.bbo["BTC"].bidPx != 0 || a.bbo["BTC"].askPx != 0) {
		t.Errorf("depth frame should NOT touch BBO state, got %+v", a.bbo["BTC"])
	}
}

func TestParse_RoutesOrderbook1ToBBO(t *testing.T) {
	a := newTestFuturesBBO()
	frame := []byte(`{"topic":"orderbook.1.BTCUSDT","type":"snapshot","data":{"s":"BTCUSDT","b":[["60050","1"]],"a":[["60055","2"]]}}`)
	snap, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if snap == nil {
		t.Fatal("BBO frame should still produce snapshot")
	}
	if a.bbo["BTC"] == nil || a.bbo["BTC"].bidPx != 60050 || a.bbo["BTC"].askPx != 60055 {
		t.Errorf("BBO state not seeded: %+v", a.bbo["BTC"])
	}
	// Depth state untouched
	if a.books["BTC"] != nil && (len(a.books["BTC"].bids) > 0 || len(a.books["BTC"].asks) > 0) {
		t.Errorf("BBO frame should NOT touch depth state")
	}
}

// ── Merge behaviour ────────────────────────────────────────────────────

func TestMergedSnapshot_BBOSplicesOverDepthTop(t *testing.T) {
	a := newTestFuturesBBO()
	// Depth: 50-level state
	_, _ = a.Parse([]byte(`{"topic":"orderbook.50.BTCUSDT","type":"snapshot","data":{"s":"BTCUSDT","b":[["60000","1"],["59999","2"]],"a":[["60100","1"],["60101","2"]]}}`))
	// BBO: strictly better top
	snap, _ := a.Parse([]byte(`{"topic":"orderbook.1.BTCUSDT","type":"snapshot","data":{"s":"BTCUSDT","b":[["60050","5"]],"a":[["60055","3"]]}}`))

	if snap.Bids[0][0] != 60050 {
		t.Errorf("BBO bid should be top, got %v", snap.Bids)
	}
	// Depth levels still present below
	if len(snap.Bids) < 3 || snap.Bids[1][0] != 60000 || snap.Bids[2][0] != 59999 {
		t.Errorf("depth bids should follow BBO: %v", snap.Bids)
	}
	if snap.Asks[0][0] != 60055 {
		t.Errorf("BBO ask should be top, got %v", snap.Asks)
	}
}

func TestMergedSnapshot_BBOSizeZeroClearsBBO(t *testing.T) {
	a := newTestFuturesBBO()
	_, _ = a.Parse([]byte(`{"topic":"orderbook.50.BTCUSDT","type":"snapshot","data":{"s":"BTCUSDT","b":[["60000","1"]],"a":[["60100","1"]]}}`))
	// BBO seed
	_, _ = a.Parse([]byte(`{"topic":"orderbook.1.BTCUSDT","type":"snapshot","data":{"s":"BTCUSDT","b":[["60050","5"]],"a":[["60055","5"]]}}`))
	// BBO size=0 — bid side evaporates
	snap, _ := a.Parse([]byte(`{"topic":"orderbook.1.BTCUSDT","type":"delta","data":{"s":"BTCUSDT","b":[["60050","0"]],"a":[]}}`))
	if snap.Bids[0][0] != 60000 {
		t.Errorf("after BBO clear, depth top should re-emerge: %v", snap.Bids)
	}
	// Ask BBO still present
	if snap.Asks[0][0] != 60055 {
		t.Errorf("ask BBO still active: %v", snap.Asks)
	}
}

func TestParse_PongAndAckIgnored(t *testing.T) {
	a := newTestFuturesBBO()
	for _, frame := range []string{
		`{"op":"pong","success":true}`,
		`{"op":"subscribe","success":true,"retMsg":"ok"}`,
	} {
		got, _ := a.Parse([]byte(frame))
		if got != nil {
			t.Errorf("%s → want nil, got %+v", frame, got)
		}
	}
}

func TestParse_NonUSDTSymbolIgnored(t *testing.T) {
	a := newTestFuturesBBO()
	got, _ := a.Parse([]byte(`{"topic":"orderbook.1.BTCUSDC","type":"snapshot","data":{"s":"BTCUSDC","b":[["60000","1"]],"a":[]}}`))
	if got != nil {
		t.Errorf("non-USDT should be nil, got %+v", got)
	}
}

func TestOnReconnect_ClearsBothStores(t *testing.T) {
	a := newTestFuturesBBO()
	a.books["BTC"] = &book{bids: map[float64]float64{60000: 1}, asks: map[float64]float64{60100: 1}}
	a.bbo["BTC"] = &bboLevel{bidPx: 60050, bidSz: 1, askPx: 60055, askSz: 1}
	a.OnReconnect()
	if len(a.books) != 0 || len(a.bbo) != 0 {
		t.Errorf("OnReconnect must clear both stores, depth=%d bbo=%d", len(a.books), len(a.bbo))
	}
}

func framesToStrings(frames [][]byte) []string {
	out := make([]string, len(frames))
	for i, f := range frames {
		out[i] = string(f)
	}
	return out
}
