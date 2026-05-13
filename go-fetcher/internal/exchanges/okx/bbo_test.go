package okx

import (
	"strings"
	"testing"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

func newTestFuturesBBO() *Futures {
	return &Futures{
		cacheKey:   "okx",
		instSuffix: "-USDT-SWAP",
		books:      make(map[string]*book),
		bbo:        make(map[string]*bboLevel),
	}
}

// ── BuildSubscribe ────────────────────────────────────────────────────

func TestBuildSubscribe_FuturesIncludesBBOTBT(t *testing.T) {
	a := newTestFuturesBBO()
	frames := a.BuildSubscribe([]string{"BTC"})
	all := string(frames[0])
	if !strings.Contains(all, `"channel":"books"`) {
		t.Errorf("missing books channel in subscribe: %s", all)
	}
	if !strings.Contains(all, `"channel":"bbo-tbt"`) {
		t.Errorf("missing bbo-tbt channel in subscribe: %s", all)
	}
}

func TestBuildSubscribe_SpotOnlyBooks(t *testing.T) {
	a := &Futures{
		cacheKey:   "okx_spot",
		instSuffix: "-USDT",
		books:      make(map[string]*book),
		bbo:        make(map[string]*bboLevel),
	}
	frames := a.BuildSubscribe([]string{"BTC"})
	all := string(frames[0])
	if !strings.Contains(all, `"channel":"books"`) {
		t.Errorf("spot should still subscribe to books: %s", all)
	}
	if strings.Contains(all, `"channel":"bbo-tbt"`) {
		t.Errorf("spot adapter MUST NOT subscribe to bbo-tbt: %s", all)
	}
}

// ── Splice helpers ────────────────────────────────────────────────────

func TestSpliceBBOBid_StrictlyBetter(t *testing.T) {
	depth := []ws.Level{{100, 1}, {99, 2}}
	got := spliceBBOBid(depth, 100.5, 5)
	if len(got) != 3 || got[0][0] != 100.5 {
		t.Errorf("BBO better should prepend: %v", got)
	}
}

func TestSpliceBBOBid_SameRefreshesSize(t *testing.T) {
	depth := []ws.Level{{100, 1}, {99, 2}}
	got := spliceBBOBid(depth, 100, 7)
	if got[0][1] != 7 {
		t.Errorf("BBO at same price should refresh size: %v", got)
	}
}

func TestSpliceBBOBid_WorseNoOp(t *testing.T) {
	depth := []ws.Level{{100, 1}}
	got := spliceBBOBid(depth, 99, 99)
	if got[0][0] != 100 {
		t.Errorf("BBO worse should leave depth: %v", got)
	}
}

func TestSpliceBBOAsk_LowerWins(t *testing.T) {
	depth := []ws.Level{{100, 1}, {101, 2}}
	got := spliceBBOAsk(depth, 99.5, 5)
	if got[0][0] != 99.5 {
		t.Errorf("lower ask should win: %v", got)
	}
}

// ── Parse routing ─────────────────────────────────────────────────────

func TestParse_RoutesBBOTBTToBBOState(t *testing.T) {
	a := newTestFuturesBBO()
	// bbo-tbt frame — same shape as books, just 1 level
	frame := []byte(`{"arg":{"channel":"bbo-tbt","instId":"BTC-USDT-SWAP"},"data":[{"bids":[["60050","5","0","1"]],"asks":[["60055","3","0","1"]]}]}`)
	snap, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if snap == nil {
		t.Fatal("BBO frame must produce snapshot")
	}
	if a.bbo["BTC"] == nil || a.bbo["BTC"].bidPx != 60050 || a.bbo["BTC"].askPx != 60055 {
		t.Errorf("BBO state not seeded: %+v", a.bbo["BTC"])
	}
	// Depth state untouched
	if a.books["BTC"] != nil && (len(a.books["BTC"].bids) > 0) {
		t.Errorf("BBO frame should NOT touch depth state")
	}
}

func TestParse_BooksFrameUntouchesBBO(t *testing.T) {
	a := newTestFuturesBBO()
	// books snapshot
	frame := []byte(`{"arg":{"channel":"books","instId":"BTC-USDT-SWAP"},"action":"snapshot","data":[{"bids":[["60000","1","0","1"]],"asks":[["60100","2","0","1"]]}]}`)
	snap, err := a.Parse(frame)
	if err != nil || snap == nil {
		t.Fatalf("parse: %v / %v", err, snap)
	}
	if a.books["BTC"] == nil || a.books["BTC"].bids[60000] != 1 {
		t.Errorf("depth state not seeded")
	}
	if a.bbo["BTC"] != nil && (a.bbo["BTC"].bidPx != 0 || a.bbo["BTC"].askPx != 0) {
		t.Errorf("depth frame should NOT seed BBO: %+v", a.bbo["BTC"])
	}
}

func TestMergedSnapshot_BBOSplicesOverDepth(t *testing.T) {
	a := newTestFuturesBBO()
	_, _ = a.Parse([]byte(`{"arg":{"channel":"books","instId":"BTC-USDT-SWAP"},"action":"snapshot","data":[{"bids":[["60000","1","0","1"]],"asks":[["60100","2","0","1"]]}]}`))
	snap, _ := a.Parse([]byte(`{"arg":{"channel":"bbo-tbt","instId":"BTC-USDT-SWAP"},"data":[{"bids":[["60050","5","0","1"]],"asks":[["60055","3","0","1"]]}]}`))
	if snap.Bids[0][0] != 60050 {
		t.Errorf("BBO bid should be top, got %v", snap.Bids)
	}
	if snap.Asks[0][0] != 60055 {
		t.Errorf("BBO ask should be top, got %v", snap.Asks)
	}
}

func TestParse_EventFrameIgnored(t *testing.T) {
	a := newTestFuturesBBO()
	got, _ := a.Parse([]byte(`{"event":"subscribe","arg":{"channel":"bbo-tbt","instId":"BTC-USDT-SWAP"}}`))
	if got != nil {
		t.Errorf("event frame → nil, got %+v", got)
	}
}

func TestParse_NonBooksNonBBOChannelIgnored(t *testing.T) {
	a := newTestFuturesBBO()
	got, _ := a.Parse([]byte(`{"arg":{"channel":"tickers","instId":"BTC-USDT-SWAP"},"data":[]}`))
	if got != nil {
		t.Errorf("non-{books,bbo-tbt} → nil, got %+v", got)
	}
}

func TestOnReconnect_ClearsBothStores(t *testing.T) {
	a := newTestFuturesBBO()
	a.books["BTC"] = &book{bids: map[float64]float64{60000: 1}, asks: map[float64]float64{60100: 1}}
	a.bbo["BTC"] = &bboLevel{bidPx: 60050, bidSz: 1, askPx: 60055, askSz: 1}
	a.OnReconnect()
	if len(a.books) != 0 || len(a.bbo) != 0 {
		t.Errorf("OnReconnect must clear both, depth=%d bbo=%d", len(a.books), len(a.bbo))
	}
}
