package bitget

import (
	"testing"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

func TestTradesParse_BuyTick(t *testing.T) {
	a := &Trades{}
	frame := []byte(`{"action":"snapshot","arg":{"instType":"USDT-FUTURES","channel":"trade","instId":"BTCUSDT"},"data":[{"ts":"1718000001000","price":"63125.5","size":"0.001","side":"buy","tradeId":"abc"}]}`)
	got, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("ticks: %d", len(got))
	}
	tk := got[0]
	if tk.Exchange != "bitget" || tk.Symbol != "BTC" {
		t.Errorf("ex/sym: %s/%s", tk.Exchange, tk.Symbol)
	}
	if tk.Side != ticks.Buy {
		t.Errorf("side=buy got %s", tk.Side)
	}
	if tk.Price != 63125.5 || tk.Size != 0.001 {
		t.Errorf("price/size: %v / %v", tk.Price, tk.Size)
	}
	if tk.TsMS != 1718000001000 {
		t.Errorf("ts (string-encoded): %d", tk.TsMS)
	}
}

func TestTradesParse_SellTick(t *testing.T) {
	a := &Trades{}
	frame := []byte(`{"action":"update","arg":{"channel":"trade","instId":"ETHUSDT"},"data":[{"ts":"1","price":"3000","size":"1","side":"sell","tradeId":"x"}]}`)
	got, _ := a.Parse(frame)
	if len(got) != 1 || got[0].Side != ticks.Sell {
		t.Errorf("side=sell got %v", got)
	}
}

func TestTradesParse_SubscribeAckIgnored(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"event":"subscribe","arg":{"channel":"trade","instId":"BTCUSDT"}}`))
	if got != nil {
		t.Errorf("subscribe-ack should produce nil, got %v", got)
	}
}

func TestTradesParse_NonTradeChannelIgnored(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"action":"snapshot","arg":{"channel":"books","instId":"BTCUSDT"},"data":[]}`))
	if got != nil {
		t.Errorf("non-trade channel should produce nil, got %v", got)
	}
}

func TestTradesParse_BatchedDataArray(t *testing.T) {
	a := &Trades{}
	frame := []byte(`{"action":"snapshot","arg":{"channel":"trade","instId":"BTCUSDT"},"data":[
		{"ts":"1","price":"60000","size":"0.1","side":"buy","tradeId":"a"},
		{"ts":"2","price":"60001","size":"0.2","side":"sell","tradeId":"b"}
	]}`)
	got, _ := a.Parse(frame)
	if len(got) != 2 {
		t.Fatalf("ticks: want 2 got %d", len(got))
	}
}
