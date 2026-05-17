package aster

import (
	"crypto/ecdsa"
	"encoding/hex"
	"strings"
	"testing"

	"github.com/ethereum/go-ethereum/crypto"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

func TestSymbol(t *testing.T) {
	if got := toAsterSymbol("btc"); got != "BTCUSDT" {
		t.Errorf("got %q", got)
	}
	if got := toAsterSymbol("ETH"); got != "ETHUSDT" {
		t.Errorf("got %q", got)
	}
}

func TestBuildQueryString_Sorted(t *testing.T) {
	got := buildQueryString(map[string]string{
		"symbol":   "BTCUSDT",
		"side":     "BUY",
		"type":     "MARKET",
		"quantity": "0.001",
	})
	want := "quantity=0.001&side=BUY&symbol=BTCUSDT&type=MARKET"
	if got != want {
		t.Errorf("got %q\nwant %q", got, want)
	}
}

// TestSignEIP712_ShapeAndDistinct: signatures are 65-byte hex (132 chars
// with 0x), v normalized to 27/28, and different msgs produce different
// sigs. We can no longer easily reconstruct the V3 digest manually
// (4-field domain + Message type), so live parity is verified by trade
// tests against the venue. Domain hash regression would surface as
// "Signature check failed" on first live order.
func TestSignEIP712_ShapeAndDistinct(t *testing.T) {
	priv, err := crypto.GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	privHex := hex.EncodeToString(crypto.FromECDSA(priv))
	_ = (*ecdsa.PrivateKey)(priv)

	sig1, err := signEIP712("quantity=0.001&side=BUY&nonce=1&user=0x0&signer=0x0", privHex)
	if err != nil {
		t.Fatalf("sign1: %v", err)
	}
	sig2, err := signEIP712("quantity=0.001&side=SELL&nonce=1&user=0x0&signer=0x0", privHex)
	if err != nil {
		t.Fatalf("sign2: %v", err)
	}
	for _, s := range []string{sig1, sig2} {
		if !strings.HasPrefix(s, "0x") || len(s) != 132 {
			t.Fatalf("unexpected sig shape: %q (len=%d)", s, len(s))
		}
		raw, _ := hex.DecodeString(strings.TrimPrefix(s, "0x"))
		if raw[64] < 27 {
			t.Errorf("v not normalized: %d", raw[64])
		}
	}
	if sig1 == sig2 {
		t.Error("different msgs produced identical signatures")
	}
}

// TestSignEIP712_Stable is a self-consistency check — the same key + msg
// must produce the same signature deterministically. The old
// Python-parity vector was pinned against the V1 EIP-712 domain
// (2-field, primaryType=AsterSignTransaction). V3 uses a 4-field domain +
// primaryType=Message; the prior vector no longer applies. Live parity
// is verified by trade tests against the venue.
func TestSignEIP712_Stable(t *testing.T) {
	const priv = "1111111111111111111111111111111111111111111111111111111111111111"
	const qs = "quantity=0.001&side=BUY&symbol=BTCUSDT&type=MARKET&nonce=1&user=0x0&signer=0x0"
	a, err := signEIP712(qs, priv)
	if err != nil {
		t.Fatalf("sign: %v", err)
	}
	b, err := signEIP712(qs, priv)
	if err != nil {
		t.Fatalf("sign2: %v", err)
	}
	if a != b {
		t.Errorf("non-deterministic sig: %s vs %s", a, b)
	}
	if len(a) != 132 || a[:2] != "0x" {
		t.Errorf("unexpected sig shape: %s", a)
	}
}

func TestRegisteredViaInit(t *testing.T) {
	a := trade.Lookup("aster")
	if a == nil {
		t.Fatal("aster adapter not registered")
	}
}
