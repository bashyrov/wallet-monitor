package lighter

import (
	"testing"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

// newTestTrades constructs a Trades with a pre-seeded idMap so we don't
// hit the live REST endpoint. Mimics the cache state after a successful
// refresh.
func newTestTrades() *Trades {
	m := newIDMap()
	m.mu.Lock()
	m.bySymb["BTC"] = 0
	m.bySymb["ETH"] = 1
	m.byID[0] = "BTC"
	m.byID[1] = "ETH"
	m.updated = time.Now()
	m.mu.Unlock()
	return &Trades{ids: m}
}

func TestTradesParse_BuyWhenIsMakerAskTrue(t *testing.T) {
	a := newTestTrades()
	// is_maker_ask=true → taker bought → Buy
	frame := []byte(`{"channel":"trade:0","type":"update/trade","nonce":1,"trades":[{"trade_id":42,"market_id":0,"size":"1.5","price":"63125.5","is_maker_ask":true,"timestamp":1718000001000}]}`)
	got, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("ticks: %d", len(got))
	}
	tk := got[0]
	if tk.Exchange != "lighter" || tk.Symbol != "BTC" {
		t.Errorf("ex/sym: %s/%s", tk.Exchange, tk.Symbol)
	}
	if tk.Side != ticks.Buy {
		t.Errorf("is_maker_ask=true → Buy, got %s", tk.Side)
	}
	if tk.Price != 63125.5 || tk.Size != 1.5 {
		t.Errorf("price/size: %v / %v", tk.Price, tk.Size)
	}
}

func TestTradesParse_SellWhenIsMakerAskFalse(t *testing.T) {
	a := newTestTrades()
	frame := []byte(`{"channel":"trade:1","type":"update/trade","trades":[{"trade_id":1,"market_id":1,"size":"1","price":"3000","is_maker_ask":false,"timestamp":1}]}`)
	got, _ := a.Parse(frame)
	if len(got) != 1 || got[0].Side != ticks.Sell {
		t.Errorf("is_maker_ask=false → Sell, got %v", got)
	}
}

func TestTradesParse_UnknownMarketIDDropped(t *testing.T) {
	a := newTestTrades()
	// market_id=999 not in idMap → silently skipped
	frame := []byte(`{"channel":"trade:999","type":"update/trade","trades":[{"trade_id":1,"market_id":999,"size":"1","price":"1","is_maker_ask":true,"timestamp":1}]}`)
	got, _ := a.Parse(frame)
	if got != nil {
		t.Errorf("unknown market_id should produce nil, got %v", got)
	}
}

func TestTradesParse_NonTradeChannelIgnored(t *testing.T) {
	a := newTestTrades()
	got, _ := a.Parse([]byte(`{"channel":"order_book:0","type":"snapshot/order_book"}`))
	if got != nil {
		t.Errorf("non-trade channel should produce nil, got %v", got)
	}
}

func TestTradesParse_BothChannelFormsAccepted(t *testing.T) {
	a := newTestTrades()
	// docs subscribe form: "trade/0"; echo form: "trade:0" — both should work
	for _, ch := range []string{"trade/0", "trade:0"} {
		frame := []byte(`{"channel":"` + ch + `","type":"update/trade","trades":[{"trade_id":1,"market_id":0,"size":"1","price":"60000","is_maker_ask":true,"timestamp":1}]}`)
		got, _ := a.Parse(frame)
		if len(got) != 1 {
			t.Errorf("channel %q should parse, got %v", ch, got)
		}
	}
}

func TestTradesParse_TradeIDStringPreferred(t *testing.T) {
	a := newTestTrades()
	frame := []byte(`{"channel":"trade:0","type":"update/trade","trades":[{"trade_id":42,"trade_id_str":"42-string","market_id":0,"size":"1","price":"60000","is_maker_ask":true,"timestamp":1}]}`)
	got, _ := a.Parse(frame)
	if len(got) != 1 || got[0].ID != "42-string" {
		t.Errorf("trade_id_str should be preferred, got %v", got)
	}
}

func TestTradesParse_TradeIDFallbackFromInt(t *testing.T) {
	a := newTestTrades()
	frame := []byte(`{"channel":"trade:0","type":"update/trade","trades":[{"trade_id":42,"market_id":0,"size":"1","price":"60000","is_maker_ask":true,"timestamp":1}]}`)
	got, _ := a.Parse(frame)
	if len(got) != 1 || got[0].ID != "42" {
		t.Errorf("trade_id fallback: want \"42\" got %q", got[0].ID)
	}
}
