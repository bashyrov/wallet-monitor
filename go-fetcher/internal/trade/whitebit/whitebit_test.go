package whitebit

import (
	"testing"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

func TestSymbol(t *testing.T) {
	if got := toWBSymbol("btc"); got != "BTC_PERP" {
		t.Errorf("got %q", got)
	}
}

func TestRegisteredViaInit(t *testing.T) {
	a := trade.Lookup("whitebit")
	if a == nil {
		t.Fatal("whitebit adapter not registered")
	}
}
