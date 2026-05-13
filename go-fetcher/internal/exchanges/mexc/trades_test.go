package mexc

import (
	"testing"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

func TestTradesParse_BuyTick(t *testing.T) {
	a := &Trades{}
	// T=1 → buy
	frame := []byte(`{"channel":"push.deal","symbol":"BTC_USDT","ts":1716000001000,"data":[{"p":63125.5,"v":100,"T":1,"O":1,"M":1,"t":1716000001000}]}`)
	got, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("ticks: want 1 got %d", len(got))
	}
	tk := got[0]
	if tk.Exchange != "mexc" || tk.Symbol != "BTC" {
		t.Errorf("ex/sym: %s/%s", tk.Exchange, tk.Symbol)
	}
	if tk.Side != ticks.Buy {
		t.Errorf("T=1 should be Buy, got %s", tk.Side)
	}
	if tk.Price != 63125.5 || tk.Size != 100 {
		t.Errorf("price/size: %v / %v", tk.Price, tk.Size)
	}
}

func TestTradesParse_SellWhenT2(t *testing.T) {
	a := &Trades{}
	frame := []byte(`{"channel":"push.deal","symbol":"ETH_USDT","data":[{"p":3000,"v":1,"T":2,"t":1}]}`)
	got, _ := a.Parse(frame)
	if len(got) != 1 || got[0].Side != ticks.Sell {
		t.Errorf("T=2 should be Sell, got %v", got)
	}
}

// Bug #4 in LIVE_ORDERBOOK_PLAN.md regression: MEXC sub.deal ack and error
// frames have `data: "<string>"` whereas push.deal has `data: [{...}]`.
// Adapter must gate on `channel` first before decoding `data`.
func TestTradesParse_SubscribeAckIgnoredAndNoErrorOnStringData(t *testing.T) {
	a := &Trades{}
	got, err := a.Parse([]byte(`{"channel":"rs.sub.deal","data":"success"}`))
	if err != nil {
		t.Fatalf("rs.sub.deal should not error (regression), got %v", err)
	}
	if got != nil {
		t.Errorf("rs.sub.deal should produce nil, got %v", got)
	}
}

func TestTradesParse_RsErrorIgnored(t *testing.T) {
	a := &Trades{}
	got, err := a.Parse([]byte(`{"channel":"rs.error","data":"some error string"}`))
	if err != nil {
		t.Fatalf("rs.error should not error, got %v", err)
	}
	if got != nil {
		t.Errorf("rs.error should produce nil, got %v", got)
	}
}

// Bug #3 regression: MEXC `data` is an array of trade objects, not a
// single map. Initial implementation expected a map and 100% of frames
// failed parse.
func TestTradesParse_DataIsArrayNotMapRegression(t *testing.T) {
	a := &Trades{}
	frame := []byte(`{"channel":"push.deal","symbol":"SOL_USDT","data":[{"p":150,"v":3,"T":1,"t":1},{"p":150.5,"v":1,"T":2,"t":2}]}`)
	got, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("array data: %v", err)
	}
	if len(got) != 2 {
		t.Fatalf("ticks: want 2 (array unwrap) got %d", len(got))
	}
	if got[0].Side != ticks.Buy || got[1].Side != ticks.Sell {
		t.Errorf("sides: want Buy,Sell got %s,%s", got[0].Side, got[1].Side)
	}
}

func TestTradesParse_NonUSDTSymbolIgnored(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"channel":"push.deal","symbol":"BTC_USDC","data":[{"p":60000,"v":1,"T":1,"t":1}]}`))
	if got != nil {
		t.Errorf("non-USDT symbol should produce nil, got %v", got)
	}
}
