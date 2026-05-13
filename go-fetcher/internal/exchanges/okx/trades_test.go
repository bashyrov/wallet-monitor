package okx

import (
	"testing"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

func newTradesT() *Trades {
	return &Trades{instSuffix: "-USDT-SWAP", exName: "okx"}
}

func TestTradesParse_BuyTick(t *testing.T) {
	a := newTradesT()
	frame := []byte(`{"arg":{"channel":"trades","instId":"BTC-USDT-SWAP"},"data":[{"instId":"BTC-USDT-SWAP","tradeId":"42","px":"63125.5","sz":"0.001","side":"buy","ts":"1718000001000"}]}`)
	got, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("ticks: want 1 got %d", len(got))
	}
	tk := got[0]
	if tk.Exchange != "okx" || tk.Symbol != "BTC" {
		t.Errorf("ex/sym: %s/%s", tk.Exchange, tk.Symbol)
	}
	if tk.Side != ticks.Buy {
		t.Errorf("side=buy should produce Buy, got %s", tk.Side)
	}
	if tk.Price != 63125.5 || tk.Size != 0.001 {
		t.Errorf("price/size: %v / %v", tk.Price, tk.Size)
	}
	if tk.ID != "42" {
		t.Errorf("id: %q", tk.ID)
	}
	if tk.TsMS != 1718000001000 {
		t.Errorf("ts: %d (OKX ts is string-encoded number)", tk.TsMS)
	}
}

func TestTradesParse_SellTick(t *testing.T) {
	a := newTradesT()
	frame := []byte(`{"arg":{"channel":"trades","instId":"ETH-USDT-SWAP"},"data":[{"instId":"ETH-USDT-SWAP","tradeId":"x","px":"3000","sz":"1","side":"sell","ts":"1"}]}`)
	got, _ := a.Parse(frame)
	if len(got) != 1 || got[0].Side != ticks.Sell {
		t.Errorf("side=sell should produce Sell, got %v", got)
	}
}

func TestTradesParse_SubscribeAckIgnored(t *testing.T) {
	a := newTradesT()
	got, _ := a.Parse([]byte(`{"event":"subscribe","arg":{"channel":"trades","instId":"BTC-USDT-SWAP"}}`))
	if got != nil {
		t.Errorf("subscribe-ack should produce nil, got %v", got)
	}
}

func TestTradesParse_NonTradesChannelIgnored(t *testing.T) {
	a := newTradesT()
	got, _ := a.Parse([]byte(`{"arg":{"channel":"books","instId":"BTC-USDT-SWAP"},"data":[]}`))
	if got != nil {
		t.Errorf("non-trades channel should produce nil, got %v", got)
	}
}

func TestTradesParse_BatchedDataArray(t *testing.T) {
	a := newTradesT()
	frame := []byte(`{"arg":{"channel":"trades","instId":"BTC-USDT-SWAP"},"data":[
		{"instId":"BTC-USDT-SWAP","tradeId":"1","px":"60000","sz":"0.1","side":"buy","ts":"1"},
		{"instId":"BTC-USDT-SWAP","tradeId":"2","px":"60001","sz":"0.2","side":"sell","ts":"2"}
	]}`)
	got, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(got) != 2 {
		t.Fatalf("ticks: want 2 got %d", len(got))
	}
}

func TestTradesParse_WrongInstSuffixIgnored(t *testing.T) {
	a := newTradesT() // expects -USDT-SWAP
	// spot frame slipping through wouldn't match
	frame := []byte(`{"arg":{"channel":"trades","instId":"BTC-USDT"},"data":[{"instId":"BTC-USDT","tradeId":"x","px":"60000","sz":"1","side":"buy","ts":"1"}]}`)
	got, _ := a.Parse(frame)
	if got != nil {
		t.Errorf("wrong inst suffix should produce nil, got %v", got)
	}
}
