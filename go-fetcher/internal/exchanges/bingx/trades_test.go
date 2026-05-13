package bingx

import (
	"testing"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

func TestTradesParse_BuyTickWhenMakerSideFalse(t *testing.T) {
	a := &Trades{}
	// m=false → buyer is taker → Buy
	frame := []byte(`{"dataType":"BTC-USDT@trade","data":[{"T":1718000001000,"s":"BTC-USDT","p":"63125.5","q":"0.001","m":false}]}`)
	got, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("ticks: %d", len(got))
	}
	tk := got[0]
	if tk.Exchange != "bingx" || tk.Symbol != "BTC" {
		t.Errorf("ex/sym: %s/%s", tk.Exchange, tk.Symbol)
	}
	if tk.Side != ticks.Buy {
		t.Errorf("m=false got %s", tk.Side)
	}
}

func TestTradesParse_SellWhenMakerSideTrue(t *testing.T) {
	a := &Trades{}
	frame := []byte(`{"dataType":"ETH-USDT@trade","data":[{"T":1,"s":"ETH-USDT","p":"3000","q":"1","m":true}]}`)
	got, _ := a.Parse(frame)
	if len(got) != 1 || got[0].Side != ticks.Sell {
		t.Errorf("m=true got %v", got)
	}
}

func TestTradesParse_NonTradeDataTypeIgnored(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"dataType":"BTC-USDT@depth","data":[]}`))
	if got != nil {
		t.Errorf("non-@trade should produce nil, got %v", got)
	}
}

func TestTradesParse_NonUSDTPairIgnored(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"dataType":"BTC-USDC@trade","data":[{"T":1,"s":"BTC-USDC","p":"60000","q":"1","m":false}]}`))
	if got != nil {
		t.Errorf("non-USDT pair should produce nil, got %v", got)
	}
}
