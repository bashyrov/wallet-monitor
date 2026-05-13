package backpack

import (
	"testing"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

// Note: BuildSubscribe hits a REST endpoint for market filter — not tested.
// Parse is pure JSON in / Tick out and is the hot-path-critical bit.

func TestTradesParse_BuyTick(t *testing.T) {
	a := &Trades{}
	// m=false → buyer is taker → Buy
	frame := []byte(`{"stream":"trade.BTC_USDC_PERP","data":{"e":"trade","E":1718000001234,"s":"BTC_USDC_PERP","p":"63125.5","q":"0.001","b":"x","a":"y","t":42,"T":1718000001230,"m":false}}`)
	got, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("ticks: %d", len(got))
	}
	tk := got[0]
	if tk.Exchange != "backpack" || tk.Symbol != "BTC" {
		t.Errorf("ex/sym: %s/%s", tk.Exchange, tk.Symbol)
	}
	if tk.Side != ticks.Buy {
		t.Errorf("m=false got %s", tk.Side)
	}
	if tk.Price != 63125.5 || tk.Size != 0.001 {
		t.Errorf("price/size: %v / %v", tk.Price, tk.Size)
	}
}

func TestTradesParse_SellWhenMakerIsBuyer(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"stream":"trade.ETH_USDC_PERP","data":{"e":"trade","s":"ETH_USDC_PERP","p":"3000","q":"1","t":1,"T":1,"m":true}}`))
	if len(got) != 1 || got[0].Side != ticks.Sell {
		t.Errorf("m=true got %v", got)
	}
}

func TestTradesParse_NonTradeStreamIgnored(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"stream":"depth.BTC_USDC_PERP","data":{"e":"depthUpdate"}}`))
	if got != nil {
		t.Errorf("non-trade stream should produce nil, got %v", got)
	}
}

func TestTradesParse_NonPerpSymbolIgnored(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"stream":"trade.BTC_USDC","data":{"e":"trade","s":"BTC_USDC","p":"60000","q":"1","t":1,"T":1,"m":false}}`))
	if got != nil {
		t.Errorf("non-_USDC_PERP suffix should produce nil, got %v", got)
	}
}
