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

// ── Helpers (pure functions, easy to pin) ────────────────────────────────

func TestToWBSymbol_UppercasesAndAppendsPERP(t *testing.T) {
	cases := map[string]string{
		"btc":  "BTC_PERP",
		"ETH":  "ETH_PERP",
		"sol":  "SOL_PERP",
	}
	for in, want := range cases {
		if got := toWBSymbol(in); got != want {
			t.Errorf("toWBSymbol(%q): want %q got %q", in, want, got)
		}
	}
}

func TestHMACHexSHA512_KnownVector(t *testing.T) {
	// SHA-512 hex digest is 128 chars.
	got := hmacHexSHA512("key", "hello")
	if len(got) != 128 {
		t.Errorf("HMAC-SHA512 hex length: want 128 got %d (%q)", len(got), got)
	}
}

func TestParseError_429MapsToRateLimit(t *testing.T) {
	err := parseError(429, []byte(`{"code":1,"message":"too many requests"}`))
	if err.Kind != trade.KindRateLimit {
		t.Errorf("429 should map to RateLimit kind, got %q", err.Kind)
	}
	if err.Message != "too many requests" {
		t.Errorf("message: %q", err.Message)
	}
}

func TestParseError_Non429MapsToExchange(t *testing.T) {
	err := parseError(400, []byte(`{"code":2,"message":"bad request"}`))
	if err.Kind != trade.KindExchange {
		t.Errorf("400 should map to Exchange kind, got %q", err.Kind)
	}
}

func TestParseError_MalformedJSONFallsBackToRawBody(t *testing.T) {
	err := parseError(400, []byte("plain text error"))
	if err.Message != "plain text error" {
		t.Errorf("raw body fallback: got %q", err.Message)
	}
}

func TestQtyString_TrimsTrailingZeros(t *testing.T) {
	cases := map[float64]string{
		1.5:         "1.5",
		1.0:         "1",
		0.001:       "0.001",
		0.0:         "0",
		0.12345678:  "0.12345678",
		1000:        "1000",
	}
	for in, want := range cases {
		if got := qtyString(in); got != want {
			t.Errorf("qtyString(%v): want %q got %q", in, want, got)
		}
	}
}
