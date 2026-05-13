package gate

import (
	"testing"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

func TestTradesParse_PositiveSizeIsBuy(t *testing.T) {
	a := &Trades{}
	// Gate convention: positive size = taker bought.
	frame := []byte(`{"channel":"futures.trades","event":"update","result":[{"id":42,"size":100,"price":"63125.5","contract":"BTC_USDT","create_time_ms":1718000001000}]}`)
	got, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("ticks: want 1 got %d", len(got))
	}
	tk := got[0]
	if tk.Exchange != "gate" || tk.Symbol != "BTC" {
		t.Errorf("ex/sym: %s/%s", tk.Exchange, tk.Symbol)
	}
	if tk.Side != ticks.Buy {
		t.Errorf("+size should be Buy, got %s", tk.Side)
	}
	if tk.Size != 100 {
		t.Errorf("size should be abs(+100)=100, got %v", tk.Size)
	}
	if tk.ID != "42" {
		t.Errorf("id: %q", tk.ID)
	}
}

func TestTradesParse_NegativeSizeIsSellAndAbsValue(t *testing.T) {
	a := &Trades{}
	frame := []byte(`{"channel":"futures.trades","event":"update","result":[{"id":1,"size":-50,"price":"3000","contract":"ETH_USDT","create_time_ms":1}]}`)
	got, _ := a.Parse(frame)
	if len(got) != 1 {
		t.Fatalf("ticks: %d", len(got))
	}
	if got[0].Side != ticks.Sell {
		t.Errorf("-size should be Sell, got %s", got[0].Side)
	}
	if got[0].Size != 50 {
		t.Errorf("size should be abs(-50)=50, got %v", got[0].Size)
	}
}

func TestTradesParse_NonUpdateIgnored(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"channel":"futures.trades","event":"subscribe","result":{"status":"success"}}`))
	if got != nil {
		t.Errorf("non-update event should produce nil, got %v", got)
	}
}

func TestTradesParse_NonFuturesChannelIgnored(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"channel":"futures.book_ticker","event":"update","result":[{"contract":"BTC_USDT","price":"60000","size":1}]}`))
	if got != nil {
		t.Errorf("non-trades channel should produce nil, got %v", got)
	}
}

func TestTradesParse_ZeroSizeFiltered(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"channel":"futures.trades","event":"update","result":[{"id":1,"size":0,"price":"60000","contract":"BTC_USDT"}]}`))
	if got != nil {
		t.Errorf("zero size should produce nil, got %v", got)
	}
}
