package kucoin

import (
	"testing"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

func TestTradesParse_BuyWithIntSize(t *testing.T) {
	a := &Trades{}
	// KuCoin Data.Size can be int OR string — common int case
	frame := []byte(`{"type":"message","topic":"/contractMarket/execution:XBTUSDTM","subject":"match","data":{"price":"63125.5","size":100,"side":"buy","ts":1718000001000,"tradeId":"abc"}}`)
	got, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("ticks: %d", len(got))
	}
	tk := got[0]
	if tk.Exchange != "kucoin" {
		t.Errorf("exchange: %s", tk.Exchange)
	}
	if tk.Symbol != "BTC" { // XBT → BTC alias
		t.Errorf("symbol: XBT should alias to BTC, got %q", tk.Symbol)
	}
	if tk.Side != ticks.Buy {
		t.Errorf("side=buy got %s", tk.Side)
	}
	if tk.Size != 100 || tk.Price != 63125.5 {
		t.Errorf("size/price: %v / %v", tk.Size, tk.Price)
	}
}

func TestTradesParse_StringSizeFormat(t *testing.T) {
	a := &Trades{}
	frame := []byte(`{"type":"message","topic":"/contractMarket/execution:ETHUSDTM","subject":"match","data":{"price":"3000","size":"50.5","side":"sell","ts":1,"tradeId":"x"}}`)
	got, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("ticks: %d", len(got))
	}
	if got[0].Size != 50.5 {
		t.Errorf("string size parse: want 50.5 got %v", got[0].Size)
	}
	if got[0].Side != ticks.Sell {
		t.Errorf("side=sell got %s", got[0].Side)
	}
}

func TestTradesParse_NonMessageTypeIgnored(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"type":"ack","id":"1"}`))
	if got != nil {
		t.Errorf("non-message should produce nil, got %v", got)
	}
}

func TestTradesParse_NonExecutionTopicIgnored(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"type":"message","topic":"/contractMarket/level2:XBTUSDTM","data":{"price":"60000","size":1,"side":"buy"}}`))
	if got != nil {
		t.Errorf("non-execution topic should produce nil, got %v", got)
	}
}
