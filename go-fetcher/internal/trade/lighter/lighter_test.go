package lighter

import (
	"context"
	"errors"
	"testing"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

func TestAccountIndex_Validation(t *testing.T) {
	a := New()
	if _, err := a.accountIndex(trade.Creds{}); err == nil {
		t.Errorf("empty index should error")
	}
	if _, err := a.accountIndex(trade.Creds{APIKey: "abc"}); err == nil {
		t.Errorf("non-numeric index should error")
	}
	if _, err := a.accountIndex(trade.Creds{APIKey: "12345"}); err != nil {
		t.Errorf("numeric index should pass: %v", err)
	}
}

func TestTradeActionsBlocked(t *testing.T) {
	a := New()
	ctx := context.Background()
	creds := trade.Creds{APIKey: "1", APISecret: "0xdead", Passphrase: "255"}

	if _, err := a.PlaceOrder(ctx, creds, trade.OpenRequest{Symbol: "BTC", Side: trade.SideBuy, Quantity: 0.001, Leverage: 1, MarginMode: trade.MarginCross}); err == nil {
		t.Errorf("PlaceOrder must reject — ZK signing not in Go")
	}
	if _, err := a.ClosePosition(ctx, creds, trade.CloseRequest{Symbol: "BTC"}); err == nil {
		t.Errorf("ClosePosition must reject — ZK signing not in Go")
	}
	if err := a.SetLeverage(ctx, creds, trade.LeverageRequest{Symbol: "BTC", Leverage: 10, MarginMode: trade.MarginCross}); err == nil {
		t.Errorf("SetLeverage must reject — ZK signing not in Go")
	}
	// And the error must be the dedicated ZK sentinel so callers can branch on it.
	_, err := a.PlaceOrder(ctx, creds, trade.OpenRequest{Symbol: "BTC", Side: trade.SideBuy, Quantity: 0.001, Leverage: 1, MarginMode: trade.MarginCross})
	if !errors.Is(err, errZK) {
		// errors.Is on a *trade.Error sentinel — same instance comparison is fine.
		if err != errZK {
			t.Errorf("expected errZK, got %v", err)
		}
	}
}

func TestRegisteredViaInit(t *testing.T) {
	a := trade.Lookup("lighter")
	if a == nil {
		t.Fatal("lighter adapter not registered")
	}
}
