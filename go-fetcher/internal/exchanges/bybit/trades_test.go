package bybit

import (
	"testing"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

func TestTradesParse_BuyTick(t *testing.T) {
	a := &Trades{}
	frame := []byte(`{"topic":"publicTrade.BTCUSDT","type":"snapshot","ts":1718000001000,"data":[{"T":1718000001000,"s":"BTCUSDT","S":"Buy","v":"0.001","p":"70000.5","L":"PlusTick","i":"abc123"}]}`)
	got, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("ticks: want 1 got %d", len(got))
	}
	tk := got[0]
	if tk.Exchange != "bybit" || tk.Symbol != "BTC" {
		t.Errorf("ex/sym: %s/%s", tk.Exchange, tk.Symbol)
	}
	if tk.Side != ticks.Buy {
		t.Errorf("S=Buy should produce Buy, got %s", tk.Side)
	}
	if tk.Price != 70000.5 || tk.Size != 0.001 {
		t.Errorf("price/size: %v / %v", tk.Price, tk.Size)
	}
	if tk.ID != "abc123" {
		t.Errorf("id: %q", tk.ID)
	}
}

func TestTradesParse_SellTick(t *testing.T) {
	a := &Trades{}
	frame := []byte(`{"topic":"publicTrade.ETHUSDT","data":[{"T":1,"s":"ETHUSDT","S":"Sell","v":"1","p":"3000","i":"x"}]}`)
	got, _ := a.Parse(frame)
	if len(got) != 1 || got[0].Side != ticks.Sell {
		t.Errorf("S=Sell should produce Sell, got %v", got)
	}
}

func TestTradesParse_BatchedDataArray(t *testing.T) {
	a := &Trades{}
	// Bybit sometimes batches multiple fills in one frame
	frame := []byte(`{"topic":"publicTrade.BTCUSDT","data":[
		{"T":1,"s":"BTCUSDT","S":"Buy","v":"0.1","p":"60000","i":"a"},
		{"T":2,"s":"BTCUSDT","S":"Sell","v":"0.2","p":"60001","i":"b"}
	]}`)
	got, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(got) != 2 {
		t.Fatalf("ticks: want 2 got %d", len(got))
	}
	if got[0].ID != "a" || got[1].ID != "b" {
		t.Errorf("ids: %s,%s", got[0].ID, got[1].ID)
	}
}

func TestTradesParse_NonTradeTopicIgnored(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"topic":"orderbook.50.BTCUSDT","data":[{"S":"Buy","v":"1","p":"1"}]}`))
	if got != nil {
		t.Errorf("non-publicTrade topic should produce nil, got %v", got)
	}
}

func TestTradesParse_PongIgnored(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"op":"pong","success":true}`))
	if got != nil {
		t.Errorf("pong should produce nil, got %v", got)
	}
}
