package htx

import (
	"testing"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

func TestTradesParse_BuyTick(t *testing.T) {
	a := &Trades{}
	frame := []byte(`{"ch":"market.BTC-USDT.trade.detail","ts":1718000001000,"tick":{"data":[{"price":63125.5,"amount":0.001,"ts":1718000001000,"id":42,"direction":"buy"}]}}`)
	got, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("ticks: %d", len(got))
	}
	tk := got[0]
	if tk.Exchange != "htx" || tk.Symbol != "BTC" {
		t.Errorf("ex/sym: %s/%s", tk.Exchange, tk.Symbol)
	}
	if tk.Side != ticks.Buy {
		t.Errorf("direction=buy got %s", tk.Side)
	}
	if tk.Price != 63125.5 || tk.Size != 0.001 {
		t.Errorf("price/size: %v / %v", tk.Price, tk.Size)
	}
}

func TestTradesParse_SellDirection(t *testing.T) {
	a := &Trades{}
	frame := []byte(`{"ch":"market.ETH-USDT.trade.detail","tick":{"data":[{"price":3000,"amount":1,"ts":1,"id":1,"direction":"sell"}]}}`)
	got, _ := a.Parse(frame)
	if len(got) != 1 || got[0].Side != ticks.Sell {
		t.Errorf("direction=sell got %v", got)
	}
}

func TestTradesParse_NonTradeChannelIgnored(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"ch":"market.BTC-USDT.depth.size_20.high_freq","tick":{"data":[]}}`))
	if got != nil {
		t.Errorf("depth channel should produce nil, got %v", got)
	}
}

func TestTradesParse_BatchedTrades(t *testing.T) {
	a := &Trades{}
	frame := []byte(`{"ch":"market.BTC-USDT.trade.detail","tick":{"data":[
		{"price":60000,"amount":0.1,"ts":1,"id":1,"direction":"buy"},
		{"price":60001,"amount":0.2,"ts":2,"id":2,"direction":"sell"}
	]}}`)
	got, _ := a.Parse(frame)
	if len(got) != 2 {
		t.Fatalf("ticks: want 2 got %d", len(got))
	}
}
