package bitget

import (
	"strings"
	"testing"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

func newTestAdapterBBO() *Adapter {
	return &Adapter{
		cacheKey: "bitget",
		instType: "USDT-FUTURES",
		books:    make(map[string]*book),
		bbo:      make(map[string]*bboLevel),
	}
}

// ── BuildSubscribe ────────────────────────────────────────────────────

func TestBuildSubscribe_FuturesIncludesBooks1(t *testing.T) {
	a := newTestAdapterBBO()
	frames := a.BuildSubscribe([]string{"BTC"})
	all := string(frames[0])
	if !strings.Contains(all, `"channel":"books15"`) {
		t.Errorf("books15 missing: %s", all)
	}
	if !strings.Contains(all, `"channel":"books1"`) {
		t.Errorf("books1 missing: %s", all)
	}
}

func TestBuildSubscribe_SpotKeepsBooks15Only(t *testing.T) {
	a := &Adapter{
		cacheKey: "bitget_spot",
		instType: "SPOT",
		books:    make(map[string]*book),
		bbo:      make(map[string]*bboLevel),
	}
	frames := a.BuildSubscribe([]string{"BTC"})
	all := string(frames[0])
	if !strings.Contains(all, `"channel":"books15"`) {
		t.Errorf("spot needs books15: %s", all)
	}
	if strings.Contains(all, `"channel":"books1"`) {
		t.Errorf("spot must NOT subscribe to books1: %s", all)
	}
}

// ── Splice helpers ────────────────────────────────────────────────────

func TestSpliceBBOBid_AllCases(t *testing.T) {
	depth := []ws.Level{{100, 1}, {99, 2}}
	if got := spliceBBOBid(depth, 100.5, 5); got[0][0] != 100.5 {
		t.Errorf("better should prepend: %v", got)
	}
	if got := spliceBBOBid(depth, 100, 7); got[0][1] != 7 {
		t.Errorf("same → refresh size")
	}
	if got := spliceBBOBid(depth, 99, 99); got[0][0] != 100 {
		t.Errorf("worse → no-op")
	}
	if got := spliceBBOBid(nil, 100, 5); len(got) != 1 || got[0][0] != 100 {
		t.Errorf("empty depth → seed: %v", got)
	}
}

func TestSpliceBBOAsk_AllCases(t *testing.T) {
	depth := []ws.Level{{100, 1}, {101, 2}}
	if got := spliceBBOAsk(depth, 99.5, 5); got[0][0] != 99.5 {
		t.Errorf("lower should prepend: %v", got)
	}
	if got := spliceBBOAsk(depth, 100, 7); got[0][1] != 7 {
		t.Errorf("same → refresh size")
	}
	if got := spliceBBOAsk(depth, 100.5, 99); got[0][0] != 100 {
		t.Errorf("worse → no-op")
	}
}

// ── Parse routing ─────────────────────────────────────────────────────

func TestParse_RoutesBooks1ToBBOState(t *testing.T) {
	a := newTestAdapterBBO()
	frame := []byte(`{"action":"snapshot","arg":{"instType":"USDT-FUTURES","channel":"books1","instId":"BTCUSDT"},"data":[{"bids":[["60050","5"]],"asks":[["60055","3"]]}]}`)
	snap, err := a.Parse(frame)
	if err != nil || snap == nil {
		t.Fatalf("parse: %v / %v", err, snap)
	}
	if a.bbo["BTC"] == nil || a.bbo["BTC"].bidPx != 60050 || a.bbo["BTC"].askPx != 60055 {
		t.Errorf("BBO state: %+v", a.bbo["BTC"])
	}
}

func TestParse_Books15UntouchesBBO(t *testing.T) {
	a := newTestAdapterBBO()
	frame := []byte(`{"action":"snapshot","arg":{"instType":"USDT-FUTURES","channel":"books15","instId":"BTCUSDT"},"data":[{"bids":[["60000","1"]],"asks":[["60100","2"]]}]}`)
	_, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if a.books["BTC"] == nil || a.books["BTC"].bids[60000] != 1 {
		t.Errorf("depth state not seeded")
	}
	if a.bbo["BTC"] != nil && (a.bbo["BTC"].bidPx != 0 || a.bbo["BTC"].askPx != 0) {
		t.Errorf("books15 frame should NOT touch BBO: %+v", a.bbo["BTC"])
	}
}

func TestMergedSnapshot_BBOSplicesOverDepth(t *testing.T) {
	a := newTestAdapterBBO()
	_, _ = a.Parse([]byte(`{"action":"snapshot","arg":{"instType":"USDT-FUTURES","channel":"books15","instId":"BTCUSDT"},"data":[{"bids":[["60000","1"]],"asks":[["60100","2"]]}]}`))
	snap, _ := a.Parse([]byte(`{"action":"snapshot","arg":{"instType":"USDT-FUTURES","channel":"books1","instId":"BTCUSDT"},"data":[{"bids":[["60050","5"]],"asks":[["60055","3"]]}]}`))
	if snap.Bids[0][0] != 60050 {
		t.Errorf("BBO bid should be top: %v", snap.Bids)
	}
	if snap.Asks[0][0] != 60055 {
		t.Errorf("BBO ask should be top: %v", snap.Asks)
	}
}

func TestParse_WrongInstTypeIgnored(t *testing.T) {
	a := newTestAdapterBBO() // expects USDT-FUTURES
	got, _ := a.Parse([]byte(`{"action":"snapshot","arg":{"instType":"SPOT","channel":"books1","instId":"BTCUSDT"},"data":[]}`))
	if got != nil {
		t.Errorf("wrong instType → nil, got %+v", got)
	}
}

func TestParse_NonBooks15Books1Ignored(t *testing.T) {
	a := newTestAdapterBBO()
	got, _ := a.Parse([]byte(`{"action":"snapshot","arg":{"instType":"USDT-FUTURES","channel":"ticker","instId":"BTCUSDT"},"data":[]}`))
	if got != nil {
		t.Errorf("non-{books15,books1} → nil, got %+v", got)
	}
}

func TestOnReconnect_ClearsBothStores(t *testing.T) {
	a := newTestAdapterBBO()
	a.books["BTC"] = &book{bids: map[float64]float64{60000: 1}, asks: map[float64]float64{60100: 1}}
	a.bbo["BTC"] = &bboLevel{bidPx: 60050, bidSz: 1, askPx: 60055, askSz: 1}
	a.OnReconnect()
	if len(a.books) != 0 || len(a.bbo) != 0 {
		t.Errorf("OnReconnect must clear both stores")
	}
}
