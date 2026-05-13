package aster

import (
	"testing"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

// Aster is a Binance USD-M fork — same wire format. Critically: same
// e/E case-collision regression. See binance/trades_test.go for the
// reference test.
func TestTradesParse_BuyTick(t *testing.T) {
	a := &Trades{}
	frame := []byte(`{"e":"trade","E":1716000001234,"T":1716000001230,"s":"BTCUSDT","t":987654,"p":"60000.5","q":"0.001","m":false}`)
	got, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("ticks: %d", len(got))
	}
	tk := got[0]
	if tk.Exchange != "aster" || tk.Symbol != "BTC" {
		t.Errorf("ex/sym: %s/%s", tk.Exchange, tk.Symbol)
	}
	if tk.Side != ticks.Buy {
		t.Errorf("m=false got %s", tk.Side)
	}
}

func TestTradesParse_SellWhenMakerIsBuyer(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"e":"trade","E":1,"T":1,"s":"ETHUSDT","t":1,"p":"3000","q":"1","m":true}`))
	if len(got) != 1 || got[0].Side != ticks.Sell {
		t.Errorf("m=true got %v", got)
	}
}

func TestTradesParse_SubscribeAckIgnored(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"result":null,"id":1}`))
	if got != nil {
		t.Errorf("subscribe-ack should produce nil, got %v", got)
	}
}

// Same case-collision regression as Binance — see binance/trades_test.go
func TestTradesParse_CaseCollisionRegression(t *testing.T) {
	a := &Trades{}
	frame := []byte(`{"e":"trade","E":1716000001234,"T":1716000001230,"s":"BTCUSDT","t":1,"p":"100","q":"1","m":false}`)
	got, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("e/E collision regression: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("ticks: %d (regression)", len(got))
	}
}
