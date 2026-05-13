package trade

import (
	"errors"
	"testing"
)

func TestError_FormattedWithCode(t *testing.T) {
	e := errExchange("60011", "auth required")
	got := e.Error()
	want := "trade.exchange[60011]: auth required"
	if got != want {
		t.Errorf("with code: want %q got %q", want, got)
	}
}

func TestError_FormattedWithoutCode(t *testing.T) {
	e := errUser("insufficient %s balance", "USDT")
	got := e.Error()
	want := "trade.user: insufficient USDT balance"
	if got != want {
		t.Errorf("no code: want %q got %q", want, got)
	}
}

func TestError_NilStringSafe(t *testing.T) {
	var e *Error
	if e.Error() != "<nil trade error>" {
		t.Errorf("nil error: %v", e.Error())
	}
}

func TestError_UnwrapPreservesCause(t *testing.T) {
	cause := errors.New("network timeout")
	e := errInternal("signing failed", cause)
	if !errors.Is(e, cause) {
		t.Errorf("errors.Is should find wrapped cause")
	}
}

func TestIsUser_True(t *testing.T) {
	if !IsUser(errUser("oops")) {
		t.Errorf("IsUser miss")
	}
	if IsUser(errExchange("X", "y")) {
		t.Errorf("IsUser false positive on exchange err")
	}
}

func TestIsExchange_True(t *testing.T) {
	if !IsExchange(errExchange("X", "y")) {
		t.Errorf("IsExchange miss")
	}
	if IsExchange(errUser("oops")) {
		t.Errorf("IsExchange false positive on user err")
	}
}

func TestIsTransient_True(t *testing.T) {
	if !IsTransient(errTransient("connection reset", nil)) {
		t.Errorf("IsTransient miss")
	}
}

func TestIsRateLimit_True(t *testing.T) {
	if !IsRateLimit(errRateLimit("429", nil)) {
		t.Errorf("IsRateLimit miss")
	}
}

func TestIsXxx_StdlibErrorAlwaysFalse(t *testing.T) {
	plain := errors.New("plain")
	if IsUser(plain) || IsExchange(plain) || IsTransient(plain) || IsRateLimit(plain) {
		t.Errorf("stdlib error should not match any kind")
	}
}

func TestIsXxx_NilAlwaysFalse(t *testing.T) {
	if IsUser(nil) || IsExchange(nil) || IsTransient(nil) || IsRateLimit(nil) {
		t.Errorf("nil should not match any kind")
	}
}

func TestKindOf_AllVariants(t *testing.T) {
	cases := []struct {
		err  error
		want ErrorKind
	}{
		{errUser("x"), KindUser},
		{errExchange("c", "x"), KindExchange},
		{errInternal("x", nil), KindInternal},
		{errRateLimit("x", nil), KindRateLimit},
		{errTransient("x", nil), KindTransient},
		{errors.New("plain"), ""},
		{nil, ""},
	}
	for _, c := range cases {
		got := kindOf(c.err)
		if got != c.want {
			t.Errorf("kindOf(%v): want %q got %q", c.err, c.want, got)
		}
	}
}
