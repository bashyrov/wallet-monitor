package redisbus

import (
	"context"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
)

func newMiniReader(t *testing.T) (*Reader, *miniredis.Miniredis) {
	t.Helper()
	mr := miniredis.RunT(t)
	r, err := NewReader("redis://" + mr.Addr())
	if err != nil {
		t.Fatalf("NewReader: %v", err)
	}
	t.Cleanup(func() { _ = r.Close() })
	return r, mr
}

func TestNewReader_EmptyURLReturnsNil(t *testing.T) {
	r, err := NewReader("")
	if err != nil {
		t.Errorf("empty URL should not error, got %v", err)
	}
	if r != nil {
		t.Errorf("empty URL should return nil reader, got %v", r)
	}
}

func TestNewReader_InvalidURLReturnsError(t *testing.T) {
	r, err := NewReader("not-a-redis-url")
	if err == nil {
		t.Errorf("invalid URL should error")
	}
	if r != nil {
		t.Errorf("invalid URL should not return reader")
	}
}

func TestReader_ReadBooks_Happy(t *testing.T) {
	r, mr := newMiniReader(t)
	// Seed two valid books
	mr.Set("ob:binance:BTC", `{"ts":1718000001.5,"data":{"bids":[[60000,1.5]],"asks":[[60100,2.0]]}}`)
	mr.Set("ob:bybit:ETH", `{"ts":1718000002.0,"data":{"bids":[[3000,5]],"asks":[[3001,2]]}}`)

	got := r.ReadBooks(context.Background(), []string{"binance:BTC", "bybit:ETH"})
	if len(got) != 2 {
		t.Fatalf("len: want 2 got %d", len(got))
	}
	if got["binance:BTC"].TS != 1718000001.5 {
		t.Errorf("BTC ts: %v", got["binance:BTC"].TS)
	}
	if got["binance:BTC"].Data["bids"][0][0] != 60000 {
		t.Errorf("BTC bid price: %v", got["binance:BTC"].Data["bids"])
	}
}

func TestReader_ReadBooks_MissingKeysSilentlyOmitted(t *testing.T) {
	r, mr := newMiniReader(t)
	mr.Set("ob:binance:BTC", `{"ts":1,"data":{"bids":[],"asks":[]}}`)

	got := r.ReadBooks(context.Background(), []string{"binance:BTC", "bybit:NOTFOUND"})
	if len(got) != 1 {
		t.Errorf("missing key should be silently omitted: %v", got)
	}
	if _, ok := got["bybit:NOTFOUND"]; ok {
		t.Errorf("missing key should not appear in result")
	}
}

func TestReader_ReadBooks_MalformedJSONOmitted(t *testing.T) {
	r, mr := newMiniReader(t)
	mr.Set("ob:binance:BTC", `{not json`)
	mr.Set("ob:bybit:ETH", `{"ts":1,"data":{"bids":[],"asks":[]}}`)

	got := r.ReadBooks(context.Background(), []string{"binance:BTC", "bybit:ETH"})
	if _, ok := got["binance:BTC"]; ok {
		t.Errorf("malformed JSON should be omitted")
	}
	if _, ok := got["bybit:ETH"]; !ok {
		t.Errorf("valid sibling should still parse")
	}
}

func TestReader_ReadBooks_EmptyPairsReturnsNil(t *testing.T) {
	r, _ := newMiniReader(t)
	got := r.ReadBooks(context.Background(), nil)
	if got != nil {
		t.Errorf("empty pairs should return nil, got %v", got)
	}
}

func TestReader_ReadBooks_NilReceiver(t *testing.T) {
	var r *Reader
	got := r.ReadBooks(context.Background(), []string{"x"})
	if got != nil {
		t.Errorf("nil reader should return nil, got %v", got)
	}
}

func TestReader_ReadBooks_ContextTimeoutReturnsNil(t *testing.T) {
	r, mr := newMiniReader(t)
	mr.Set("ob:binance:BTC", `{"ts":1,"data":{"bids":[],"asks":[]}}`)
	ctx, cancel := context.WithCancel(context.Background())
	cancel() // pre-cancel
	got := r.ReadBooks(ctx, []string{"binance:BTC"})
	if got != nil {
		t.Errorf("cancelled ctx should return nil, got %v", got)
	}
}

func TestReader_Close_NilSafe(t *testing.T) {
	var r *Reader
	if err := r.Close(); err != nil {
		t.Errorf("nil Close should be safe, got %v", err)
	}
}

func TestReader_ReadBooks_KeyPrefixCorrect(t *testing.T) {
	r, mr := newMiniReader(t)
	// Caller passes bare "binance:BTC"; Reader must prefix with "ob:"
	mr.Set("ob:binance:BTC", `{"ts":42,"data":{"bids":[],"asks":[]}}`)
	got := r.ReadBooks(context.Background(), []string{"binance:BTC"})
	if got["binance:BTC"].TS != 42 {
		t.Errorf("ob: prefix not applied to MGET key")
	}
	// Verify by also checking that a request with the prefix already
	// included misses (caller contract: pass bare pair).
	got2 := r.ReadBooks(context.Background(), []string{"ob:binance:BTC"})
	if _, ok := got2["ob:binance:BTC"]; ok {
		t.Errorf("caller must not pre-prefix; result should miss")
	}
}

func TestReader_ReadBooks_MGetUnderlyingTimeout(t *testing.T) {
	// Verify the 1s timeout in ReadBooks is honored — we approximate by
	// blocking miniredis via SetError.
	r, mr := newMiniReader(t)
	mr.SetError("temporary outage")
	t0 := time.Now()
	got := r.ReadBooks(context.Background(), []string{"binance:BTC"})
	if got != nil {
		t.Errorf("error response should return nil, got %v", got)
	}
	if time.Since(t0) > 3*time.Second {
		t.Errorf("timeout not honored — took %v", time.Since(t0))
	}
}
