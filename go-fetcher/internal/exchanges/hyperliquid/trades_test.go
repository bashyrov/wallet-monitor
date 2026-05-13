package hyperliquid

import (
	"testing"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

func TestTradesParse_BuyTickWhenSideB(t *testing.T) {
	a := &Trades{}
	// side="B" = bid (taker bought from ask) → Buy
	frame := []byte(`{"channel":"trades","data":[{"coin":"BTC","side":"B","px":"60000","sz":"1.5","hash":"0xabc","time":1718000001000,"tid":42}]}`)
	got, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("ticks: want 1 got %d", len(got))
	}
	tk := got[0]
	if tk.Exchange != "hyperliquid" || tk.Symbol != "BTC" {
		t.Errorf("ex/sym: %s/%s", tk.Exchange, tk.Symbol)
	}
	if tk.Side != ticks.Buy {
		t.Errorf("side=B should produce Buy, got %s", tk.Side)
	}
	if tk.Price != 60000 || tk.Size != 1.5 {
		t.Errorf("price/size: %v / %v", tk.Price, tk.Size)
	}
	if tk.ID != "42" {
		t.Errorf("id: %q", tk.ID)
	}
}

func TestTradesParse_SellTickWhenSideA(t *testing.T) {
	a := &Trades{}
	// side="A" = ask (taker sold into bid) → Sell
	frame := []byte(`{"channel":"trades","data":[{"coin":"ETH","side":"A","px":"3000","sz":"2","time":1,"tid":1}]}`)
	got, _ := a.Parse(frame)
	if len(got) != 1 || got[0].Side != ticks.Sell {
		t.Errorf("side=A should produce Sell, got %v", got)
	}
}

func TestTradesParse_NonTradesChannelIgnored(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"channel":"l2Book","data":{"coin":"BTC"}}`))
	if got != nil {
		t.Errorf("non-trades channel should produce nil, got %v", got)
	}
}

func TestTradesParse_BatchedDataArray(t *testing.T) {
	a := &Trades{}
	frame := []byte(`{"channel":"trades","data":[
		{"coin":"SOL","side":"B","px":"150","sz":"3","time":1,"tid":1},
		{"coin":"SOL","side":"A","px":"150.5","sz":"1","time":2,"tid":2}
	]}`)
	got, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(got) != 2 {
		t.Fatalf("ticks: want 2 got %d", len(got))
	}
	if got[0].Side != ticks.Buy || got[1].Side != ticks.Sell {
		t.Errorf("sides: %s,%s", got[0].Side, got[1].Side)
	}
}

func TestTradesParse_CoinCasing(t *testing.T) {
	a := &Trades{}
	// HL sends `coin` as-is; we normalize to uppercase
	frame := []byte(`{"channel":"trades","data":[{"coin":"btc","side":"B","px":"60000","sz":"1","time":1,"tid":1}]}`)
	got, _ := a.Parse(frame)
	if len(got) != 1 || got[0].Symbol != "BTC" {
		t.Errorf("symbol should be uppercased, got %q", got[0].Symbol)
	}
}

func TestTradesParse_ZeroSizeFiltered(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"channel":"trades","data":[{"coin":"BTC","side":"B","px":"60000","sz":"0","time":1,"tid":1}]}`))
	if got != nil {
		t.Errorf("zero-size trade should produce nil, got %v", got)
	}
}
