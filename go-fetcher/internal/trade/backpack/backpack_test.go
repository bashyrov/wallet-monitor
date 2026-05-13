package backpack

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"testing"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

func TestSymbol(t *testing.T) {
	if got := toBPSymbol("btc"); got != "BTC_USDC_PERP" {
		t.Errorf("got %q", got)
	}
}

func TestBuildSignString_Canonical(t *testing.T) {
	got := buildSignString("orderExecute", 1234567890, map[string]string{
		"symbol": "BTC_USDC_PERP", "side": "Bid", "orderType": "Market",
	})
	want := "instruction=orderExecute&orderType=Market&side=Bid&symbol=BTC_USDC_PERP&timestamp=1234567890&window=60000"
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

func TestToBPSymbol_UppercasesAndAppendsUSDCPERP(t *testing.T) {
	cases := map[string]string{
		"btc": "BTC_USDC_PERP",
		"ETH": "ETH_USDC_PERP",
		"sol": "SOL_USDC_PERP",
	}
	for in, want := range cases {
		if got := toBPSymbol(in); got != want {
			t.Errorf("toBPSymbol(%q): want %q got %q", in, want, got)
		}
	}
}

func TestBuildSignString_KeysSortedAlphabetically(t *testing.T) {
	got := buildSignString("test", 100, map[string]string{
		"z": "1", "a": "2", "m": "3",
	})
	// instruction=test&a=2&m=3&z=1&timestamp=100&window=60000
	want := "instruction=test&a=2&m=3&z=1&timestamp=100&window=60000"
	if got != want {
		t.Errorf("sort: want %q got %q", want, got)
	}
}

func TestSignEd25519_InvalidBase64SeedErrors(t *testing.T) {
	if _, err := signEd25519("msg", "!@#$ not_base64"); err == nil {
		t.Errorf("invalid base64 should error")
	}
}

func TestSignEd25519_WrongSeedLengthErrors(t *testing.T) {
	// Ed25519 seed must be exactly 32 bytes. base64 of "x" is too short.
	if _, err := signEd25519("msg", base64.StdEncoding.EncodeToString([]byte("too short"))); err == nil {
		t.Errorf("wrong seed length should error")
	}
}

func TestParseError_429MapsToRateLimit(t *testing.T) {
	err := parseError(429, []byte(`{"message":"rate limited"}`))
	if err.Kind != trade.KindRateLimit {
		t.Errorf("429 → RateLimit, got %q", err.Kind)
	}
}

func TestParseError_PrefersMessageOverError(t *testing.T) {
	// Backpack response has both `message` and `error`; adapter prefers `message`.
	err := parseError(400, []byte(`{"message":"bad sig","error":"E_BAD"}`))
	if err.Message != "bad sig" {
		t.Errorf("prefer message: got %q", err.Message)
	}
}

func TestQtyString_TrimsTrailingZeros(t *testing.T) {
	cases := map[float64]string{
		1.5: "1.5", 1.0: "1", 0.0: "0", 0.001: "0.001",
	}
	for in, want := range cases {
		if got := qtyString(in); got != want {
			t.Errorf("qtyString(%v): want %q got %q", in, want, got)
		}
	}
}
