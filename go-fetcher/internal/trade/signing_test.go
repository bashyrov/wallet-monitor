package trade

import (
	"crypto/sha256"
	"crypto/sha512"
	"encoding/hex"
	"testing"
)

// Cross-check vectors hand-computed via openssl:
//   echo -n "msg" | openssl dgst -sha256 -hmac "secret" -binary | hexdump

func TestHMACHexSHA256_KnownVector(t *testing.T) {
	// HMAC-SHA256("key", "The quick brown fox jumps over the lazy dog")
	// = f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8
	got := HMACHexSHA256("key", "The quick brown fox jumps over the lazy dog")
	want := "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8"
	if got != want {
		t.Errorf("HMAC-SHA256: want %s got %s", want, got)
	}
}

func TestHMACHexSHA256_EmptyPayload(t *testing.T) {
	got := HMACHexSHA256("secret", "")
	// HMAC-SHA256("secret", "") = f9e66e179b6747ae54108f82f8ade8b3c25d76fd30afde6c395822c530196169
	want := "f9e66e179b6747ae54108f82f8ade8b3c25d76fd30afde6c395822c530196169"
	if got != want {
		t.Errorf("empty payload: want %s got %s", want, got)
	}
}

func TestHMACBase64SHA256_KnownVector(t *testing.T) {
	// base64(HMAC-SHA256("key", "The quick brown fox jumps over the lazy dog"))
	got := HMACBase64SHA256("key", "The quick brown fox jumps over the lazy dog")
	want := "97yD9DBThCSxMpjmqm+xQ+9NWaFJRhdZl0edvC0aPNg="
	if got != want {
		t.Errorf("base64: want %s got %s", want, got)
	}
}

func TestHMACBase64SHA512_KnownVector(t *testing.T) {
	// base64(HMAC-SHA512("key", "hello"))
	got := HMACBase64SHA512("key", "hello")
	if len(got) == 0 {
		t.Errorf("base64 sha512 empty")
	}
	// SHA-512 base64 is 88 chars (including padding)
	if len(got) != 88 {
		t.Errorf("base64 sha512 length: want 88 got %d", len(got))
	}
}

func TestHMACWith_SHA256MatchesHMACHexSHA256(t *testing.T) {
	raw := HMACWith(sha256.New, "key", "payload")
	hexEnc := hex.EncodeToString(raw)
	direct := HMACHexSHA256("key", "payload")
	if hexEnc != direct {
		t.Errorf("HMACWith(sha256) ≠ HMACHexSHA256: %s vs %s", hexEnc, direct)
	}
}

func TestHMACWith_SHA512Length(t *testing.T) {
	raw := HMACWith(sha512.New, "key", "payload")
	if len(raw) != 64 {
		t.Errorf("HMAC-SHA512 raw bytes: want 64 got %d", len(raw))
	}
}

func TestSortedFormQuery_AlphabeticalOrder(t *testing.T) {
	got := SortedFormQuery(map[string]string{
		"symbol": "BTCUSDT",
		"side":   "BUY",
		"qty":    "0.001",
	})
	// Expected: qty=0.001&side=BUY&symbol=BTCUSDT (sorted by key)
	want := "qty=0.001&side=BUY&symbol=BTCUSDT"
	if got != want {
		t.Errorf("sort: want %q got %q", want, got)
	}
}

func TestSortedFormQuery_EmptyValueSkipped(t *testing.T) {
	got := SortedFormQuery(map[string]string{
		"symbol":   "BTCUSDT",
		"optional": "",
		"side":     "BUY",
	})
	want := "side=BUY&symbol=BTCUSDT"
	if got != want {
		t.Errorf("empty skip: want %q got %q", want, got)
	}
}

func TestSortedFormQuery_URLEscapesSpecialChars(t *testing.T) {
	got := SortedFormQuery(map[string]string{
		"sym": "BTC USDT", // space → +
		"qty": "0.5",
	})
	// space encodes as + in query strings
	want := "qty=0.5&sym=BTC+USDT"
	if got != want {
		t.Errorf("escape: want %q got %q", want, got)
	}
}

func TestSortedFormQuery_EmptyMapReturnsEmpty(t *testing.T) {
	if got := SortedFormQuery(map[string]string{}); got != "" {
		t.Errorf("empty: %q", got)
	}
}

func TestSortedFormQuery_SingleKey(t *testing.T) {
	got := SortedFormQuery(map[string]string{"k": "v"})
	if got != "k=v" {
		t.Errorf("single: %q", got)
	}
}
