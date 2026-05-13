package kraken

import (
	"testing"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

func TestTradesParse_BuyTick(t *testing.T) {
	a := &Trades{}
	frame := []byte(`{"feed":"trade","product_id":"PF_XBTUSD","uid":"abc-uuid","side":"buy","type":"fill","seq":42,"time":1718000001000,"qty":0.001,"price":63125.5}`)
	got, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("ticks: %d", len(got))
	}
	tk := got[0]
	if tk.Exchange != "kraken" || tk.Symbol != "BTC" { // XBT alias
		t.Errorf("ex/sym: %s/%s (expect kraken/BTC via XBT alias)", tk.Exchange, tk.Symbol)
	}
	if tk.Side != ticks.Buy {
		t.Errorf("side=buy got %s", tk.Side)
	}
	if tk.Price != 63125.5 || tk.Size != 0.001 {
		t.Errorf("price/size: %v / %v", tk.Price, tk.Size)
	}
	if tk.ID != "abc-uuid" {
		t.Errorf("id (uid): %q", tk.ID)
	}
}

func TestTradesParse_SellTick(t *testing.T) {
	a := &Trades{}
	frame := []byte(`{"feed":"trade","product_id":"PF_ETHUSD","uid":"x","side":"sell","time":1,"qty":1,"price":3000}`)
	got, _ := a.Parse(frame)
	if len(got) != 1 || got[0].Side != ticks.Sell {
		t.Errorf("side=sell got %v", got)
	}
	if got[0].Symbol != "ETH" {
		t.Errorf("symbol: ETH got %q", got[0].Symbol)
	}
}

func TestTradesParse_NonProductFramesIgnored(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"event":"subscribed","feed":"trade","product_ids":["PF_XBTUSD"]}`))
	if got != nil {
		t.Errorf("event frame should produce nil, got %v", got)
	}
}

func TestTradesParse_NonTradeFeedIgnored(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"feed":"book","product_id":"PF_XBTUSD","side":"buy","price":60000,"qty":0.001}`))
	if got != nil {
		t.Errorf("non-trade feed should produce nil, got %v", got)
	}
}
