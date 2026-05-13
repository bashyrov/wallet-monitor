package ethereal

import (
	"testing"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

func TestTradesParse_BuySide0(t *testing.T) {
	a := &Trades{}
	// sd=0 → Buy per current convention (may be flipped after live verify)
	frame := []byte(`{"e":"TradeFill","t":1718000001230,"data":{"s":"BTCUSD","t":1718000001230,"d":[{"id":"uuid-1","px":"63125.5","sz":"0.001","sd":0,"t":1718000001230}]}}`)
	got, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("ticks: %d", len(got))
	}
	tk := got[0]
	if tk.Exchange != "ethereal" || tk.Symbol != "BTC" {
		t.Errorf("ex/sym: %s/%s", tk.Exchange, tk.Symbol)
	}
	if tk.Side != ticks.Buy {
		t.Errorf("sd=0 got %s", tk.Side)
	}
	if tk.Price != 63125.5 || tk.Size != 0.001 {
		t.Errorf("price/size: %v / %v", tk.Price, tk.Size)
	}
}

func TestTradesParse_SellSide1(t *testing.T) {
	a := &Trades{}
	frame := []byte(`{"e":"TradeFill","t":1,"data":{"s":"ETHUSD","d":[{"id":"x","px":"3000","sz":"1","sd":1,"t":1}]}}`)
	got, _ := a.Parse(frame)
	if len(got) != 1 || got[0].Side != ticks.Sell {
		t.Errorf("sd=1 got %v", got)
	}
}

func TestTradesParse_NonTradeFillIgnored(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"e":"Subscribed","data":{"s":"BTCUSD"}}`))
	if got != nil {
		t.Errorf("non-TradeFill should produce nil, got %v", got)
	}
}

func TestTradesParse_NonUSDSymbolIgnored(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"e":"TradeFill","data":{"s":"BTCUSDT","d":[{"px":"60000","sz":"1","sd":0,"t":1}]}}`))
	if got != nil {
		t.Errorf("non-USD suffix should produce nil, got %v", got)
	}
}

func TestTradesParse_BatchedFills(t *testing.T) {
	a := &Trades{}
	frame := []byte(`{"e":"TradeFill","t":1,"data":{"s":"BTCUSD","t":1,"d":[
		{"id":"a","px":"60000","sz":"0.1","sd":0,"t":1},
		{"id":"b","px":"60001","sz":"0.2","sd":1,"t":2}
	]}}`)
	got, _ := a.Parse(frame)
	if len(got) != 2 {
		t.Fatalf("ticks: want 2 got %d", len(got))
	}
}
