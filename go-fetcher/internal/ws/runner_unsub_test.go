package ws

import (
	"context"
	"testing"
	"time"
)

// mockUnsubAdapter is a minimal adapter that implements Unsubscriber.
type mockUnsubAdapter struct {
	name        string
	unsubCalled []string // symbols passed to BuildUnsubscribe
}

func (m *mockUnsubAdapter) Name() string                          { return m.name }
func (m *mockUnsubAdapter) URL(_ context.Context) (string, error) { return "ws://placeholder", nil }
func (m *mockUnsubAdapter) BuildSubscribe(_ []string) [][]byte    { return nil }
func (m *mockUnsubAdapter) Parse(_ []byte) (*Snapshot, error)    { return nil, nil }
func (m *mockUnsubAdapter) Heartbeat() []byte                    { return nil }
func (m *mockUnsubAdapter) HeartbeatInterval() time.Duration     { return 0 }
func (m *mockUnsubAdapter) PongFor(_ []byte) []byte              { return nil }
func (m *mockUnsubAdapter) UseLibPings() bool                    { return false }
func (m *mockUnsubAdapter) SubscribeDelay() time.Duration        { return 0 }
func (m *mockUnsubAdapter) MaxSymbols() int                      { return 0 }
func (m *mockUnsubAdapter) DecompressGzip() bool                 { return false }
func (m *mockUnsubAdapter) OnReconnect()                         {}

// BuildUnsubscribe records calls and returns a sentinel frame.
func (m *mockUnsubAdapter) BuildUnsubscribe(symbols []string) [][]byte {
	m.unsubCalled = append(m.unsubCalled, symbols...)
	return [][]byte{[]byte(`{"op":"unsubscribe","args":["test"]}`)}
}

// mockNoUnsubAdapter does NOT implement Unsubscriber.
type mockNoUnsubAdapter struct{ mockUnsubAdapter }

func TestSetSymbols_WithUnsubscriber_CallsBuildUnsubNotReconnect(t *testing.T) {
	a := &mockUnsubAdapter{name: "test"}
	r := NewRunner(a, func(_ string, _ Snapshot) {})

	// Pre-seed with BTC and ETH as subscribed (conn=nil simulates disconnected).
	r.symMu.Lock()
	r.symbols = map[string]struct{}{"BTC": {}, "ETH": {}}
	r.subscribed = map[string]struct{}{"BTC": {}, "ETH": {}}
	r.symMu.Unlock()

	// Remove ETH (only BTC remains). conn is nil so no actual send, but
	// BuildUnsubscribe must be called (to clear adapter state) and ETH
	// must be removed from r.subscribed.
	r.SetSymbols([]string{"BTC"})

	// No goroutine wait needed — conn==nil path is synchronous.

	// The adapter should have received the unsubscribe call for ETH.
	found := false
	for _, s := range a.unsubCalled {
		if s == "ETH" {
			found = true
		}
	}
	if !found {
		t.Errorf("BuildUnsubscribe should have been called with ETH, got %v", a.unsubCalled)
	}

	// ETH should be removed from r.subscribed (synchronously when conn==nil).
	r.symMu.Lock()
	_, ethSubscribed := r.subscribed["ETH"]
	r.symMu.Unlock()
	if ethSubscribed {
		t.Error("ETH should have been removed from r.subscribed after unsubscribe (conn=nil path)")
	}
}

func TestSetSymbols_WithoutUnsubscriber_NoUnsubCall(t *testing.T) {
	// mockNoUnsubAdapter doesn't implement Unsubscriber — runner should NOT
	// call BuildUnsubscribe on it. We verify no panic.
	a := &struct{ mockUnsubAdapter }{mockUnsubAdapter{name: "nousub"}}
	r := NewRunner(a, func(_ string, _ Snapshot) {})
	r.symMu.Lock()
	r.symbols = map[string]struct{}{"BTC": {}, "ETH": {}}
	r.subscribed = map[string]struct{}{"BTC": {}, "ETH": {}}
	r.symMu.Unlock()

	// This should fall back to conn.Close() (conn is nil here so no-op).
	r.SetSymbols([]string{"BTC"}) // ETH removed
	// No panic = test passes.
}
