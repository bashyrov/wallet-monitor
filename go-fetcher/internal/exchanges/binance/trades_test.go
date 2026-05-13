package binance

import (
	"testing"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

func TestTradesParse_BuyTick(t *testing.T) {
	a := &Trades{}
	// m=false → buyer is taker → Buy
	frame := []byte(`{"e":"trade","E":1716000001234,"T":1716000001230,"s":"BTCUSDT","t":987654,"p":"60000.5","q":"0.001","m":false}`)
	got, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("ticks: want 1 got %d", len(got))
	}
	tk := got[0]
	if tk.Exchange != "binance" {
		t.Errorf("exchange: %s", tk.Exchange)
	}
	if tk.Symbol != "BTC" {
		t.Errorf("symbol: want BTC got %s", tk.Symbol)
	}
	if tk.Side != ticks.Buy {
		t.Errorf("side: m=false should be Buy got %s", tk.Side)
	}
	if tk.Price != 60000.5 || tk.Size != 0.001 {
		t.Errorf("price/size: %v / %v", tk.Price, tk.Size)
	}
	if tk.TsMS != 1716000001230 {
		t.Errorf("ts: %d", tk.TsMS)
	}
	if tk.ID != "987654" {
		t.Errorf("id: %q", tk.ID)
	}
}

func TestTradesParse_SellWhenBuyerIsMaker(t *testing.T) {
	a := &Trades{}
	// m=true → buyer is maker → taker is seller → Sell
	frame := []byte(`{"e":"trade","E":1,"T":1,"s":"ETHUSDT","t":1,"p":"3000","q":"1","m":true}`)
	got, _ := a.Parse(frame)
	if len(got) != 1 || got[0].Side != ticks.Sell {
		t.Errorf("m=true should produce Sell, got %v", got)
	}
}

func TestTradesParse_SubscribeAckIgnored(t *testing.T) {
	a := &Trades{}
	got, err := a.Parse([]byte(`{"result":null,"id":1}`))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if got != nil {
		t.Errorf("subscribe-ack should produce nil, got %v", got)
	}
}

func TestTradesParse_NonTradeEventIgnored(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"e":"kline","s":"BTCUSDT"}`))
	if got != nil {
		t.Errorf("non-trade event should produce nil, got %v", got)
	}
}

func TestTradesParse_NonUSDTSymbolIgnored(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"e":"trade","E":1,"T":1,"s":"BTCBUSD","t":1,"p":"60000","q":"0.001","m":false}`))
	if got != nil {
		t.Errorf("non-USDT symbol should produce nil, got %v", got)
	}
}

func TestTradesParse_ZeroPriceFiltered(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"e":"trade","E":1,"T":1,"s":"BTCUSDT","t":1,"p":"0","q":"0.001","m":false}`))
	if got != nil {
		t.Errorf("zero price should produce nil, got %v", got)
	}
}

// Regression test for bug #5 in LIVE_ORDERBOOK_PLAN.md: Binance wire has
// both lowercase "e" (event type string) and uppercase "E" (event time
// number). Without an explicit EvTime int64 field bound to "E", sonic
// falls back to case-insensitive matching and routes the number into
// the string field, failing the unmarshal silently.
func TestTradesParse_CaseInsensitiveCollisionRegression(t *testing.T) {
	a := &Trades{}
	frame := []byte(`{"e":"trade","E":1716000001234,"T":1716000001230,"s":"BTCUSDT","t":1,"p":"100","q":"1","m":false}`)
	got, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse failed on legitimate frame — case-collision regression: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("ticks: want 1 got %d (case-collision regression)", len(got))
	}
}
