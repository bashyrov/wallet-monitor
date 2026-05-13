package ws

import (
	"errors"
	"net"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

func TestIsPolicyClose_KnownPolicyCode(t *testing.T) {
	for _, code := range []int{1008, 1011, 3001, 4400, 4401} {
		err := &websocket.CloseError{Code: code, Text: "test"}
		if !IsPolicyClose(err) {
			t.Errorf("code %d should be policy", code)
		}
	}
}

func TestIsPolicyClose_NormalCloseNotPolicy(t *testing.T) {
	for _, code := range []int{1000, 1001, 1006, 1009, 4200} {
		err := &websocket.CloseError{Code: code, Text: "test"}
		if IsPolicyClose(err) {
			t.Errorf("code %d should NOT be policy", code)
		}
	}
}

func TestIsPolicyClose_NonWebSocketError(t *testing.T) {
	if IsPolicyClose(errors.New("network unreachable")) {
		t.Errorf("plain errors must NOT be policy")
	}
	if IsPolicyClose(&net.OpError{Op: "dial", Err: errors.New("conn refused")}) {
		t.Errorf("net.OpError must NOT be policy")
	}
}

func TestIsPolicyClose_NilError(t *testing.T) {
	if IsPolicyClose(nil) {
		t.Errorf("nil error must NOT be policy")
	}
}

func TestIsPolicyClose_WrappedError(t *testing.T) {
	// errors.As must unwrap — adapters sometimes wrap CloseError with %w
	wrapped := errFromWrapped(&websocket.CloseError{Code: 1008})
	if !IsPolicyClose(wrapped) {
		t.Errorf("wrapped CloseError(1008) should be policy")
	}
}

type wrappedErr struct{ err error }

func (w *wrappedErr) Error() string { return "wrapped: " + w.err.Error() }
func (w *wrappedErr) Unwrap() error { return w.err }

func errFromWrapped(inner error) error { return &wrappedErr{err: inner} }

func TestBackoff_TransientStartsAt300ms(t *testing.T) {
	var b Backoff
	first := b.NextTransient()
	if first != 300*time.Millisecond {
		t.Errorf("first transient: want 300ms got %v", first)
	}
}

func TestBackoff_TransientDoubles(t *testing.T) {
	var b Backoff
	a := b.NextTransient()       // 300ms
	c := b.NextTransient()       // 600ms
	d := b.NextTransient()       // 1.2s
	if c != 2*a {
		t.Errorf("transient doubling: %v → %v", a, c)
	}
	if d != 2*c {
		t.Errorf("transient doubling: %v → %v", c, d)
	}
}

func TestBackoff_TransientCapsAt30s(t *testing.T) {
	var b Backoff
	// Run many iterations to hit the cap
	for i := 0; i < 20; i++ {
		_ = b.NextTransient()
	}
	d := b.NextTransient()
	if d != 30*time.Second {
		t.Errorf("transient cap: want 30s got %v", d)
	}
}

func TestBackoff_PolicyStartsAt30s(t *testing.T) {
	var b Backoff
	first := b.NextPolicy()
	if first != 30*time.Second {
		t.Errorf("first policy: want 30s got %v", first)
	}
}

func TestBackoff_PolicyCapsAt5min(t *testing.T) {
	var b Backoff
	for i := 0; i < 10; i++ {
		_ = b.NextPolicy()
	}
	d := b.NextPolicy()
	if d != 5*time.Minute {
		t.Errorf("policy cap: want 5min got %v", d)
	}
}

func TestBackoff_ResetTransientDoesNotResetPolicy(t *testing.T) {
	// The two curves are independent — successful subscribe-ack resets
	// transient but NOT policy. Policy resets only on a data frame.
	var b Backoff
	_ = b.NextPolicy() // 30s
	_ = b.NextPolicy() // 60s
	b.ResetTransient()
	if b.policyCur == 30*time.Second {
		t.Errorf("ResetTransient should NOT reset policy (still %v)", b.policyCur)
	}
}

func TestBackoff_ResetPolicyResetsCurve(t *testing.T) {
	var b Backoff
	_ = b.NextPolicy() // 30s
	_ = b.NextPolicy() // 60s
	_ = b.NextPolicy() // 120s
	b.ResetPolicy()
	// Next should start back at policyStart
	d := b.NextPolicy()
	if d != 30*time.Second {
		t.Errorf("after ResetPolicy: want 30s got %v", d)
	}
}

func TestBackoff_TransientResetGoesBackToStart(t *testing.T) {
	var b Backoff
	_ = b.NextTransient() // 300ms
	_ = b.NextTransient() // 600ms
	b.ResetTransient()
	d := b.NextTransient()
	if d != 300*time.Millisecond {
		t.Errorf("after ResetTransient: want 300ms got %v", d)
	}
}
