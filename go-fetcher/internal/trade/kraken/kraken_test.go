package kraken

import (
	"encoding/base64"
	"testing"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

func TestSymbolMapping(t *testing.T) {
	if got := toKrakenSymbol("btc"); got != "PF_XBTUSD" {
		t.Errorf("got %q", got)
	}
	if got := toKrakenSymbol("eth"); got != "PF_ETHUSD" {
		t.Errorf("got %q", got)
	}
	if got := fromKrakenSymbol("PF_XBTUSD"); got != "BTC" {
		t.Errorf("got %q", got)
	}
}

func TestSign(t *testing.T) {
	// Reference: secret is base64-encoded random bytes; signature must
	// be a valid base64 string.
	secret := base64.StdEncoding.EncodeToString([]byte("abcdef0123456789abcdef0123456789"))
	sig, err := krakenSign(secret, "size=1&symbol=PF_BTCUSD", "1234567890", "/sendorder")
	if err != nil {
		t.Fatalf("sign failed: %v", err)
	}
	if _, err := base64.StdEncoding.DecodeString(sig); err != nil {
		t.Errorf("expected valid base64: %v", err)
	}
}

func TestRegisteredViaInit(t *testing.T) {
	a := trade.Lookup("kraken")
	if a == nil {
		t.Fatal("kraken adapter not registered")
	}
}
