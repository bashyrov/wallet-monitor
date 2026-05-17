package lighter

import (
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

func TestRegisteredViaInit(t *testing.T) {
	a := trade.Lookup("lighter")
	if a == nil {
		t.Fatal("lighter adapter not registered")
	}
}
