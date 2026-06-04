package redisbus

import (
	"encoding/json"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

func newMiniWriter(t *testing.T, throttle time.Duration) (*Writer, *miniredis.Miniredis) {
	t.Helper()
	mr := miniredis.RunT(t)
	w, err := NewWriter("redis://"+mr.Addr(), throttle)
	if err != nil {
		t.Fatalf("NewWriter: %v", err)
	}
	t.Cleanup(func() { _ = w.Close() })
	return w, mr
}

func TestNewWriter_EmptyURLReturnsNil(t *testing.T) {
	w, err := NewWriter("", 50*time.Millisecond)
	if err != nil {
		t.Errorf("empty URL should not error, got %v", err)
	}
	if w != nil {
		t.Errorf("empty URL should return nil writer, got %v", w)
	}
}

func TestNewWriter_InvalidURLReturnsError(t *testing.T) {
	w, err := NewWriter("not-a-url", 50*time.Millisecond)
	if err == nil {
		t.Errorf("invalid URL should error")
	}
	if w != nil {
		t.Errorf("invalid URL should not return writer")
	}
}

func TestNewWriter_DefaultThrottleWhenZero(t *testing.T) {
	w, _ := newMiniWriter(t, 0)
	if w.throttle != 50*time.Millisecond {
		t.Errorf("zero throttle should default to 50ms, got %v", w.throttle)
	}
}

func TestWriter_WriteBook_StoresKey(t *testing.T) {
	w, mr := newMiniWriter(t, 0)
	w.WriteBook("binance", "BTC", []ws.Level{{60000, 1.5}}, []ws.Level{{60100, 2.0}})
	raw, err := mr.Get("ob:binance:BTC")
	if err != nil {
		t.Fatalf("key not set: %v", err)
	}
	var entry struct {
		TS   float64                `json:"ts"`
		Data map[string][][]float64 `json:"data"`
	}
	if err := json.Unmarshal([]byte(raw), &entry); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if entry.TS == 0 {
		t.Errorf("ts not set")
	}
	if entry.Data["bids"][0][0] != 60000 {
		t.Errorf("bid price: %v", entry.Data["bids"])
	}
}

func TestWriter_WriteBook_KeyShapeMatchesPython(t *testing.T) {
	w, mr := newMiniWriter(t, 0)
	w.WriteBook("bybit", "ETH", []ws.Level{{3000, 5}}, []ws.Level{{3001, 2}})
	keys := mr.Keys()
	want := "ob:bybit:ETH"
	found := false
	for _, k := range keys {
		if k == want {
			found = true
		}
	}
	if !found {
		t.Errorf("expected key %q in %v", want, keys)
	}
}

func TestWriter_WriteBook_RespectsTTL(t *testing.T) {
	w, mr := newMiniWriter(t, 0)
	w.WriteBook("okx", "SOL", []ws.Level{{150, 1}}, nil)
	ttl := mr.TTL("ob:okx:SOL")
	// obTTL is 10s in writer.go
	if ttl < 5*time.Second || ttl > 11*time.Second {
		t.Errorf("TTL: want ~10s got %v", ttl)
	}
}

func TestWriter_WriteBook_ThrottleSkipsRepeatedWrites(t *testing.T) {
	w, mr := newMiniWriter(t, 50*time.Millisecond)
	// First write goes through
	w.WriteBook("binance", "BTC", []ws.Level{{60000, 1}}, nil)
	raw1, _ := mr.Get("ob:binance:BTC")
	// Second write within throttle window — should be skipped
	w.WriteBook("binance", "BTC", []ws.Level{{60100, 2}}, nil)
	raw2, _ := mr.Get("ob:binance:BTC")
	if raw1 != raw2 {
		t.Errorf("throttle should have skipped 2nd write, but value changed:\n  %s\n  %s", raw1, raw2)
	}
}

func TestWriter_WriteBook_ThrottleAllowsAfterWindow(t *testing.T) {
	w, mr := newMiniWriter(t, 5*time.Millisecond)
	w.WriteBook("binance", "BTC", []ws.Level{{60000, 1}}, nil)
	raw1, _ := mr.Get("ob:binance:BTC")
	time.Sleep(20 * time.Millisecond) // > throttle
	w.WriteBook("binance", "BTC", []ws.Level{{60100, 2}}, nil)
	raw2, _ := mr.Get("ob:binance:BTC")
	if raw1 == raw2 {
		t.Errorf("after throttle window, write should succeed (raw1==raw2)")
	}
}

func TestWriter_WriteBook_NilReceiverNoOp(t *testing.T) {
	var w *Writer
	// Must not panic
	w.WriteBook("binance", "BTC", nil, nil)
}

func TestWriter_Close_NilSafe(t *testing.T) {
	var w *Writer
	if err := w.Close(); err != nil {
		t.Errorf("nil Close should be safe, got %v", err)
	}
}

func TestFloatTS_FormatPythonStyle(t *testing.T) {
	// FloatTS produces Python-style epoch seconds float with 6 decimals.
	ts := time.UnixMilli(1718000028123)
	got := FloatTS(ts)
	want := "1718000028.123000"
	if got != want {
		t.Errorf("FloatTS: want %q got %q", want, got)
	}
}
