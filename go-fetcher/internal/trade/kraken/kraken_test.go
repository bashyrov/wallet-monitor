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

func TestSymbolMapping_RoundTrip(t *testing.T) {
	cases := []string{"BTC", "ETH", "SOL", "ATOM"}
	for _, sym := range cases {
		pf := toKrakenSymbol(sym)
		got := fromKrakenSymbol(pf)
		if got != sym {
			t.Errorf("roundtrip %q → %q → %q", sym, pf, got)
		}
	}
}

func TestSymbolMapping_XBTAlias(t *testing.T) {
	// BTC ↔ XBT alias both directions.
	if got := toKrakenSymbol("BTC"); got != "PF_XBTUSD" {
		t.Errorf("BTC → XBT: got %q", got)
	}
	if got := fromKrakenSymbol("PF_XBTUSD"); got != "BTC" {
		t.Errorf("XBT → BTC: got %q", got)
	}
	// Non-BTC tokens left alone.
	if got := toKrakenSymbol("ETH"); got != "PF_ETHUSD" {
		t.Errorf("ETH should not be aliased: got %q", got)
	}
}

func TestSign_BadBase64Secret(t *testing.T) {
	// Secret must be base64; garbage rejected.
	_, err := krakenSign("not_b64!@#$", "x=1", "1", "/y")
	if err == nil {
		t.Errorf("invalid base64 secret should produce error")
	}
}

func TestParseError_429MapsToRateLimit(t *testing.T) {
	err := parseError(429, []byte(`{"error":"rate limit exceeded"}`))
	if err.Kind != trade.KindRateLimit {
		t.Errorf("429 → RateLimit, got %q", err.Kind)
	}
	if err.Message != "rate limit exceeded" {
		t.Errorf("message: %q", err.Message)
	}
}

func TestParseError_Non429MapsToExchange(t *testing.T) {
	err := parseError(400, []byte(`{"error":"bad nonce"}`))
	if err.Kind != trade.KindExchange {
		t.Errorf("400 → Exchange, got %q", err.Kind)
	}
}

func TestParseError_MalformedJSONFallsBackToRawBody(t *testing.T) {
	err := parseError(500, []byte("internal server error"))
	if err.Message != "internal server error" {
		t.Errorf("fallback to raw body: %q", err.Message)
	}
}

func TestQtyString_FormatTrimming(t *testing.T) {
	cases := map[float64]string{
		1.0:        "1",
		1.5:        "1.5",
		0.0:        "0",
		0.00000001: "0.00000001",
	}
	for in, want := range cases {
		if got := qtyString(in); got != want {
			t.Errorf("qtyString(%v): want %q got %q", in, want, got)
		}
	}
}
