package funding

import (
	"bytes"
	"compress/gzip"
	"testing"
)

// Regression: previous byteReader was a value receiver with no position
// state — Read() returned the same bytes on every call, causing gzip
// reader to either loop forever or duplicate output. Switched to
// bytes.NewReader (stdlib) which tracks position correctly.
func TestGunzip_RoundTrip(t *testing.T) {
	original := []byte(`{"ch":"market.BTC-USDT.depth","tick":{"bids":[[60000,1.5]],"asks":[]}}`)
	var buf bytes.Buffer
	zw := gzip.NewWriter(&buf)
	if _, err := zw.Write(original); err != nil {
		t.Fatalf("compress: %v", err)
	}
	_ = zw.Close()

	got, err := gunzip(buf.Bytes())
	if err != nil {
		t.Fatalf("gunzip: %v", err)
	}
	if string(got) != string(original) {
		t.Errorf("roundtrip mismatch:\n  want %s\n  got  %s", original, got)
	}
}

func TestGunzip_LargePayloadRoundTrip(t *testing.T) {
	// HTX/BingX can send multi-KB frames — verify position tracking
	// holds across multiple Read calls inside gzip.Reader.
	original := bytes.Repeat([]byte("BTCUSDT"), 5000) // 35 KB
	var buf bytes.Buffer
	zw := gzip.NewWriter(&buf)
	_, _ = zw.Write(original)
	_ = zw.Close()

	got, err := gunzip(buf.Bytes())
	if err != nil {
		t.Fatalf("large gunzip: %v", err)
	}
	if !bytes.Equal(got, original) {
		t.Errorf("large roundtrip: len want %d got %d", len(original), len(got))
	}
}

func TestGunzip_InvalidDataReturnsError(t *testing.T) {
	_, err := gunzip([]byte("not gzip"))
	if err == nil {
		t.Errorf("invalid gzip should produce error")
	}
}

func TestGunzip_EmptyInputReturnsError(t *testing.T) {
	_, err := gunzip([]byte{})
	if err == nil {
		t.Errorf("empty input should produce error")
	}
}
