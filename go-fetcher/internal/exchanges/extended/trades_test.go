package extended

import (
	"testing"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

func TestTradesParse_BuyTrade(t *testing.T) {
	a := &Trades{}
	frame := []byte(`{"ts":1718000001000,"seq":42,"data":[{"m":"BTC-USD","S":"BUY","tT":"TRADE","T":1718000001000,"p":"63125.5","q":"0.001","i":12345}]}`)
	got, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("ticks: %d", len(got))
	}
	tk := got[0]
	if tk.Exchange != "extended" || tk.Symbol != "BTC" {
		t.Errorf("ex/sym: %s/%s", tk.Exchange, tk.Symbol)
	}
	if tk.Side != ticks.Buy {
		t.Errorf("S=BUY got %s", tk.Side)
	}
	if tk.Price != 63125.5 || tk.Size != 0.001 {
		t.Errorf("price/size: %v / %v", tk.Price, tk.Size)
	}
}

func TestTradesParse_SellTrade(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"ts":1,"data":[{"m":"ETH-USD","S":"SELL","tT":"TRADE","T":1,"p":"3000","q":"1","i":1}]}`))
	if len(got) != 1 || got[0].Side != ticks.Sell {
		t.Errorf("S=SELL got %v", got)
	}
}

func TestTradesParse_LiquidationAccepted(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"ts":1,"data":[{"m":"BTC-USD","S":"BUY","tT":"LIQUIDATION","T":1,"p":"60000","q":"0.1","i":1}]}`))
	if len(got) != 1 {
		t.Errorf("LIQUIDATION should produce a tick, got %v", got)
	}
}

func TestTradesParse_EmptyDataIgnored(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"ts":1,"data":[]}`))
	if got != nil {
		t.Errorf("empty data should produce nil, got %v", got)
	}
}

func TestTradesParse_MultiMarketFanOut(t *testing.T) {
	a := &Trades{}
	// Path-omit subscription mode fan-outs all markets on one socket
	frame := []byte(`{"ts":1,"data":[
		{"m":"BTC-USD","S":"BUY","tT":"TRADE","T":1,"p":"60000","q":"0.1","i":1},
		{"m":"ETH-USD","S":"SELL","tT":"TRADE","T":2,"p":"3000","q":"1","i":2},
		{"m":"SOL-USD","S":"BUY","tT":"TRADE","T":3,"p":"150","q":"5","i":3}
	]}`)
	got, _ := a.Parse(frame)
	if len(got) != 3 {
		t.Fatalf("multi-market fan-out: want 3 got %d", len(got))
	}
	if got[0].Symbol != "BTC" || got[1].Symbol != "ETH" || got[2].Symbol != "SOL" {
		t.Errorf("symbols: %s,%s,%s", got[0].Symbol, got[1].Symbol, got[2].Symbol)
	}
}
