package backpack

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"testing"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

func TestSymbol(t *testing.T) {
	if got := toBPSymbol("btc"); got != "BTC_USDT" {
		t.Errorf("got %q", got)
	}
}

func TestBuildSignString_Canonical(t *testing.T) {
	got := buildSignString("orderExecute", 1234567890, map[string]string{
		"symbol": "BTC_USDT", "side": "Bid", "orderType": "Market",
	})
	want := "instruction=orderExecute&orderType=Market&side=Bid&symbol=BTC_USDT&timestamp=1234567890&window=60000"
	if got != want {
		t.Errorf("got\n %q\nwant\n %q", got, want)
	}
}

func TestSignEd25519(t *testing.T) {
	// Generate a real ed25519 key pair and sign a known string.
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	seed := priv.Seed()
	seedB64 := base64.StdEncoding.EncodeToString(seed)
	msg := "instruction=test&timestamp=1&window=60000"

	sigB64, err := signEd25519(msg, seedB64)
	if err != nil {
		t.Fatalf("sign failed: %v", err)
	}
	sig, err := base64.StdEncoding.DecodeString(sigB64)
	if err != nil {
		t.Fatalf("expected base64 sig: %v", err)
	}
	if !ed25519.Verify(pub, []byte(msg), sig) {
		t.Errorf("signature failed verification")
	}
}

func TestRegisteredViaInit(t *testing.T) {
	a := trade.Lookup("backpack")
	if a == nil {
		t.Fatal("backpack adapter not registered")
	}
}
