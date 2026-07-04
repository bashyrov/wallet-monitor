package upbit

import (
	"strings"
	"testing"
)

func newTestSpot() *Spot { return &Spot{subs: make(map[string]struct{})} }

func TestSpot_BuildSubscribe_ArrayFrameWithUSDTCodes(t *testing.T) {
	a := newTestSpot()
	frames := a.BuildSubscribe([]string{"BTC", "eth"})
	if len(frames) != 1 {
		t.Fatalf("want single frame (Upbit replace-semantics), got %d", len(frames))
	}
	s := string(frames[0])
	if !strings.HasPrefix(s, "[") {
		t.Errorf("subscribe frame must be a JSON array: %s", s)
	}
	for _, want := range []string{`"type":"orderbook"`, `"USDT-BTC"`, `"USDT-ETH"`, `"ticket"`, `"format":"DEFAULT"`} {
		if !strings.Contains(s, want) {
			t.Errorf("frame missing %q: %s", want, s)
		}
	}
}

func TestSpot_BuildSubscribe_CumulativeUnion(t *testing.T) {
	// Runner delta-subscribes only ADDED symbols; Upbit replaces the whole
	// subscription per frame. The adapter must re-emit the full union.
	a := newTestSpot()
	_ = a.BuildSubscribe([]string{"BTC"})
	frames := a.BuildSubscribe([]string{"ETH"})
	s := string(frames[0])
	if !strings.Contains(s, `"USDT-BTC"`) || !strings.Contains(s, `"USDT-ETH"`) {
		t.Errorf("second frame must carry the union: %s", s)
	}
	a.OnReconnect()
	frames = a.BuildSubscribe([]string{"SOL"})
	if strings.Contains(string(frames[0]), "USDT-BTC") {
		t.Errorf("OnReconnect must clear the cumulative set: %s", frames[0])
	}
}

func TestSpot_Parse_SnapshotSortedBestFirst(t *testing.T) {
	a := newTestSpot()
	frame := []byte(`{
		"type":"orderbook","code":"USDT-BTC","timestamp":1700000000123,
		"total_ask_size":12.3,"total_bid_size":8.4,
		"orderbook_units":[
			{"ask_price":60000.0,"bid_price":59999.5,"ask_size":1.2,"bid_size":0.8},
			{"ask_price":60001.0,"bid_price":59999.0,"ask_size":2.0,"bid_size":1.5}
		]}`)
	snap, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if snap == nil || snap.Symbol != "BTC" {
		t.Fatalf("snap wrong: %+v", snap)
	}
	if len(snap.Bids) != 2 || len(snap.Asks) != 2 {
		t.Fatalf("want 2 levels per side, got %d/%d", len(snap.Bids), len(snap.Asks))
	}
	if snap.Bids[0][0] != 59999.5 || snap.Bids[0][1] != 0.8 {
		t.Errorf("best bid wrong: %v", snap.Bids[0])
	}
	if snap.Asks[0][0] != 60000.0 || snap.Asks[0][1] != 1.2 {
		t.Errorf("best ask wrong: %v", snap.Asks[0])
	}
	if snap.Bids[0][0] < snap.Bids[1][0] {
		t.Errorf("bids not best-first: %v", snap.Bids)
	}
	if snap.Asks[0][0] > snap.Asks[1][0] {
		t.Errorf("asks not best-first: %v", snap.Asks)
	}
	if snap.EventTime.UnixMilli() != 1700000000123 {
		t.Errorf("EventTime wrong: %v", snap.EventTime)
	}
}

func TestSpot_Parse_NonUSDTMarketIgnored(t *testing.T) {
	a := newTestSpot()
	snap, _ := a.Parse([]byte(`{"type":"orderbook","code":"KRW-BTC","orderbook_units":[{"ask_price":1,"bid_price":1,"ask_size":1,"bid_size":1}]}`))
	if snap != nil {
		t.Errorf("KRW market must be ignored, got %+v", snap)
	}
}

func TestSpot_Parse_PingStatusIgnored(t *testing.T) {
	a := newTestSpot()
	snap, err := a.Parse([]byte(`{"status":"UP"}`))
	if snap != nil || err != nil {
		t.Errorf("status frame → nil,nil; got %+v %v", snap, err)
	}
}

func TestTrades_Parse_BidIsBuy(t *testing.T) {
	a := &Trades{subs: make(map[string]struct{})}
	frame := []byte(`{"type":"trade","code":"USDT-BTC","trade_price":60000.0,"trade_volume":0.01,"ask_bid":"BID","trade_timestamp":1700000000000,"sequential_id":17000000001}`)
	tks, err := a.Parse(frame)
	if err != nil || len(tks) != 1 {
		t.Fatalf("parse: %v %v", tks, err)
	}
	tk := tks[0]
	if tk.Exchange != "upbit" || tk.Symbol != "BTC" || tk.Price != 60000.0 || tk.Size != 0.01 {
		t.Errorf("tick wrong: %+v", tk)
	}
	if tk.Side != "B" {
		t.Errorf("BID must map to Buy, got %q", tk.Side)
	}
	if tk.TsMS != 1700000000000 || tk.ID != "17000000001" {
		t.Errorf("ts/id wrong: %+v", tk)
	}
}

func TestTrades_Parse_AskIsSell(t *testing.T) {
	a := &Trades{subs: make(map[string]struct{})}
	tks, _ := a.Parse([]byte(`{"type":"trade","code":"USDT-ETH","trade_price":3000,"trade_volume":1,"ask_bid":"ASK","trade_timestamp":1700000000000}`))
	if len(tks) != 1 || tks[0].Side != "S" {
		t.Errorf("ASK must map to Sell: %+v", tks)
	}
}
