package whitebit

import (
	"testing"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

// WhiteBIT trade format: params is [market, [trades]] (positional array).
// Note time is float seconds; converted to ms internally.
func TestTradesParse_BuyTick(t *testing.T) {
	a := &Trades{}
	frame := []byte(`{"method":"trades_update","params":["BTC_PERP",[{"id":42,"time":1718000001.5,"price":"63125.5","amount":"0.001","type":"buy"}]]}`)
	got, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("ticks: %d", len(got))
	}
	tk := got[0]
	if tk.Exchange != "whitebit" || tk.Symbol != "BTC" {
		t.Errorf("ex/sym: %s/%s", tk.Exchange, tk.Symbol)
	}
	if tk.Side != ticks.Buy {
		t.Errorf("type=buy got %s", tk.Side)
	}
	if tk.Price != 63125.5 || tk.Size != 0.001 {
		t.Errorf("price/size: %v / %v", tk.Price, tk.Size)
	}
	// 1718000001.5 sec → 1718000001500 ms
	if tk.TsMS != 1718000001500 {
		t.Errorf("time sec→ms: want 1718000001500 got %d", tk.TsMS)
	}
}

func TestTradesParse_SellTick(t *testing.T) {
	a := &Trades{}
	frame := []byte(`{"method":"trades_update","params":["ETH_PERP",[{"id":1,"time":1,"price":"3000","amount":"1","type":"sell"}]]}`)
	got, _ := a.Parse(frame)
	if len(got) != 1 || got[0].Side != ticks.Sell {
		t.Errorf("type=sell got %v", got)
	}
}

func TestTradesParse_NonTradeMethodIgnored(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"method":"depth_update","params":["BTC_PERP",{}]}`))
	if got != nil {
		t.Errorf("non-trades method should produce nil, got %v", got)
	}
}

func TestTradesParse_NonPerpMarketIgnored(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"method":"trades_update","params":["BTC_USDT",[{"id":1,"time":1,"price":"60000","amount":"1","type":"buy"}]]}`))
	if got != nil {
		t.Errorf("non-_PERP market should produce nil, got %v", got)
	}
}

func TestTradesParse_BatchedTrades(t *testing.T) {
	a := &Trades{}
	frame := []byte(`{"method":"trades_update","params":["BTC_PERP",[
		{"id":1,"time":1,"price":"60000","amount":"0.1","type":"buy"},
		{"id":2,"time":2,"price":"60001","amount":"0.2","type":"sell"}
	]]}`)
	got, _ := a.Parse(frame)
	if len(got) != 2 {
		t.Fatalf("ticks: want 2 got %d", len(got))
	}
}

func TestTradesParse_NumericPriceAccepted(t *testing.T) {
	a := &Trades{}
	// some venues send numeric (not string) prices; our switch handles both
	frame := []byte(`{"method":"trades_update","params":["BTC_PERP",[{"id":1,"time":1,"price":60000,"amount":0.1,"type":"buy"}]]}`)
	got, _ := a.Parse(frame)
	if len(got) != 1 || got[0].Price != 60000 {
		t.Errorf("numeric price: %v", got)
	}
}
