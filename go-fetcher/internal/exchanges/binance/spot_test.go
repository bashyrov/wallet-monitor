package binance

import (
	"strings"
	"testing"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

func newTestSpot() *Spot {
	return &Spot{
		depthState: make(map[string][2][]ws.Level),
		bboState:   make(map[string]spotBBO),
	}
}

func TestSpot_BuildSubscribe_DepthOnly(t *testing.T) {
	// HOTFIX 2026-05-13: bookTicker dropped from spot subscribe set
	// for the same reason it was dropped from futures: 2 streams per
	// symbol pushes Binance over its per-conn cap and trips 1008.
	a := newTestSpot()
	frames := a.BuildSubscribe([]string{"BTC", "ETH"})
	if len(frames) == 0 {
		t.Fatal("no frames")
	}
	s := string(frames[0])
	for _, want := range []string{"btcusdt@depth20@100ms", "ethusdt@depth20@100ms"} {
		if !strings.Contains(s, want) {
			t.Errorf("frame missing %q: %s", want, s)
		}
	}
	if strings.Contains(s, "@bookTicker") {
		t.Errorf("frame must NOT include @bookTicker post-hotfix: %s", s)
	}
}

func TestSpot_Parse_DepthFramePopulatesDepth(t *testing.T) {
	a := newTestSpot()
	frame := []byte(`{"stream":"btcusdt@depth20@100ms","data":{"bids":[["60000","1.5"]],"asks":[["60100","2.0"]]}}`)
	snap, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if snap == nil || snap.Symbol != "BTC" {
		t.Fatalf("snap wrong: %+v", snap)
	}
	if a.depthState["BTC"][0][0][0] != 60000 {
		t.Errorf("depth state not seeded")
	}
	if !snap.EventTime.IsZero() {
		t.Errorf("depth-only frame should not produce EventTime")
	}
}

func TestSpot_Parse_BookTickerSetsEventTime(t *testing.T) {
	a := newTestSpot()
	// Spot bookTicker WITH E (some endpoints carry it). Plain version
	// without E should leave EventTime zero but populate BBO state.
	frame := []byte(`{"stream":"btcusdt@bookTicker","data":{"b":"60050","B":"5","a":"60055","A":"3","E":1700000000000}}`)
	snap, _ := a.Parse(frame)
	if snap == nil {
		t.Fatal("bookTicker must emit")
	}
	if snap.EventTime.IsZero() {
		t.Error("E field should populate EventTime")
	}
	if a.bboState["BTC"].bidPx != 60050 || a.bboState["BTC"].askPx != 60055 {
		t.Errorf("BBO state not seeded: %+v", a.bboState["BTC"])
	}
}

func TestSpot_MergedSnapshot_BBOOverDepth(t *testing.T) {
	a := newTestSpot()
	// Seed depth then push tighter BBO.
	_, _ = a.Parse([]byte(`{"stream":"btcusdt@depth20@100ms","data":{"bids":[["60000","1"]],"asks":[["60100","2"]]}}`))
	snap, _ := a.Parse([]byte(`{"stream":"btcusdt@bookTicker","data":{"b":"60050","B":"5","a":"60055","A":"3","E":1700000000000}}`))
	if snap.Bids[0][0] != 60050 {
		t.Errorf("BBO bid 60050 should be top, got %v", snap.Bids)
	}
	if snap.Asks[0][0] != 60055 {
		t.Errorf("BBO ask 60055 should be top, got %v", snap.Asks)
	}
}

func TestSpot_SpliceBid_AllCases(t *testing.T) {
	depth := []ws.Level{{100, 1}}
	if got := spliceSpotBid(depth, 100.5, 5); got[0][0] != 100.5 {
		t.Errorf("better prepends")
	}
	if got := spliceSpotBid(depth, 100, 7); got[0][1] != 7 {
		t.Errorf("same refreshes size")
	}
	if got := spliceSpotBid(depth, 99, 99); got[0][0] != 100 {
		t.Errorf("worse no-op")
	}
	if got := spliceSpotBid(nil, 100, 5); len(got) != 1 || got[0][0] != 100 {
		t.Errorf("empty depth seeds")
	}
	if got := spliceSpotBid(depth, 0, 0); got[0][0] != 100 {
		t.Errorf("zero BBO no-op")
	}
}

func TestSpot_SpliceAsk_AllCases(t *testing.T) {
	depth := []ws.Level{{100, 1}}
	if got := spliceSpotAsk(depth, 99.5, 5); got[0][0] != 99.5 {
		t.Errorf("lower prepends")
	}
	if got := spliceSpotAsk(depth, 100, 7); got[0][1] != 7 {
		t.Errorf("same refreshes")
	}
	if got := spliceSpotAsk(depth, 100.5, 99); got[0][0] != 100 {
		t.Errorf("higher no-op")
	}
}

func TestSpot_Parse_SubscribeAckIgnored(t *testing.T) {
	a := newTestSpot()
	got, _ := a.Parse([]byte(`{"result":null,"id":1}`))
	if got != nil {
		t.Errorf("ack → nil, got %+v", got)
	}
}

func TestSpot_OnReconnect_ClearsBothStores(t *testing.T) {
	a := newTestSpot()
	a.depthState["BTC"] = [2][]ws.Level{{{60000, 1}}, {{60100, 1}}}
	a.bboState["BTC"] = spotBBO{bidPx: 60050, askPx: 60055}
	a.OnReconnect()
	if len(a.depthState) != 0 || len(a.bboState) != 0 {
		t.Errorf("OnReconnect must clear both")
	}
}
