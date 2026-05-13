package paradex

import (
	"testing"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

func TestTradesParse_BuyTick(t *testing.T) {
	a := &Trades{}
	frame := []byte(`{"jsonrpc":"2.0","method":"subscription","params":{"channel":"trades.BTC-USD-PERP","data":{"created_at":1718000001000,"id":"trade123","market":"BTC-USD-PERP","price":"42000.50","side":"BUY","size":"1.5","trade_type":"FILL"}}}`)
	got, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("ticks: %d", len(got))
	}
	tk := got[0]
	if tk.Exchange != "paradex" || tk.Symbol != "BTC" {
		t.Errorf("ex/sym: %s/%s", tk.Exchange, tk.Symbol)
	}
	if tk.Side != ticks.Buy {
		t.Errorf("side=BUY got %s", tk.Side)
	}
	if tk.Price != 42000.50 || tk.Size != 1.5 {
		t.Errorf("price/size: %v / %v", tk.Price, tk.Size)
	}
	if tk.ID != "trade123" {
		t.Errorf("id: %q", tk.ID)
	}
}

func TestTradesParse_SellTick(t *testing.T) {
	a := &Trades{}
	frame := []byte(`{"jsonrpc":"2.0","method":"subscription","params":{"channel":"trades.ETH-USD-PERP","data":{"market":"ETH-USD-PERP","price":"3000","side":"SELL","size":"1","trade_type":"FILL"}}}`)
	got, _ := a.Parse(frame)
	if len(got) != 1 || got[0].Side != ticks.Sell {
		t.Errorf("side=SELL got %v", got)
	}
}

func TestTradesParse_LiquidationAccepted(t *testing.T) {
	a := &Trades{}
	frame := []byte(`{"jsonrpc":"2.0","method":"subscription","params":{"channel":"trades.BTC-USD-PERP","data":{"market":"BTC-USD-PERP","price":"60000","side":"BUY","size":"0.1","trade_type":"LIQUIDATION"}}}`)
	got, _ := a.Parse(frame)
	if len(got) != 1 {
		t.Errorf("LIQUIDATION should produce a tick, got %v", got)
	}
}

func TestTradesParse_NonFillTradeTypeIgnored(t *testing.T) {
	a := &Trades{}
	// TRANSFER / SETTLE_MARKET / RPI / BLOCK_TRADE should NOT produce ticks
	frame := []byte(`{"jsonrpc":"2.0","method":"subscription","params":{"channel":"trades.BTC-USD-PERP","data":{"market":"BTC-USD-PERP","price":"60000","side":"BUY","size":"1","trade_type":"TRANSFER"}}}`)
	got, _ := a.Parse(frame)
	if got != nil {
		t.Errorf("TRANSFER should produce nil, got %v", got)
	}
}

func TestTradesParse_SubscribeResultIgnored(t *testing.T) {
	a := &Trades{}
	got, _ := a.Parse([]byte(`{"jsonrpc":"2.0","id":1,"result":{"channel":"trades.BTC-USD-PERP"}}`))
	if got != nil {
		t.Errorf("subscribe result should produce nil, got %v", got)
	}
}
