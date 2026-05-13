package trade

import (
	"context"
	"testing"
)

// stubAdapter — minimal Adapter for registry tests.
type stubAdapter struct{ name string }

func (s *stubAdapter) Name() string { return s.name }
func (s *stubAdapter) PlaceOrder(context.Context, Creds, OpenRequest) (*Result, error) {
	return &Result{}, nil
}
func (s *stubAdapter) ClosePosition(context.Context, Creds, CloseRequest) (*Result, error) {
	return &Result{}, nil
}
func (s *stubAdapter) SetLeverage(context.Context, Creds, LeverageRequest) error { return nil }
func (s *stubAdapter) ListPositions(context.Context, Creds, string) ([]Position, error) {
	return nil, nil
}
func (s *stubAdapter) GetBalance(context.Context, Creds) (*Balance, error) { return &Balance{}, nil }

func TestRegistry_RegisterAndLookup(t *testing.T) {
	// Use a unique name so we don't collide with real adapters registered
	// via init() of imported packages.
	a := &stubAdapter{name: "test-venue-xyz"}
	Register("test-venue-xyz", a)

	got := Lookup("test-venue-xyz")
	if got == nil {
		t.Fatalf("Lookup miss after Register")
	}
	if got.Name() != "test-venue-xyz" {
		t.Errorf("name: %q", got.Name())
	}
}

func TestRegistry_LookupCaseInsensitive(t *testing.T) {
	Register("UpperCase-test", &stubAdapter{name: "uppercase-test"})
	if got := Lookup("uppercase-test"); got == nil {
		t.Errorf("lookup with lowercased name should find")
	}
}

func TestRegistry_LookupMissReturnsNil(t *testing.T) {
	if got := Lookup("no-such-venue-zzz"); got != nil {
		t.Errorf("Lookup miss should return nil, got %v", got)
	}
}

func TestRegistry_RegisterReplacesExisting(t *testing.T) {
	first := &stubAdapter{name: "first"}
	second := &stubAdapter{name: "second"}
	Register("replace-test", first)
	Register("replace-test", second)

	got := Lookup("replace-test")
	if got.Name() != "second" {
		t.Errorf("re-register should replace: got name %q", got.Name())
	}
}

func TestRegistry_RegisterNilPanics(t *testing.T) {
	defer func() {
		if r := recover(); r == nil {
			t.Errorf("Register(nil) should panic")
		}
	}()
	Register("nil-test", nil)
}

func TestRegistry_SupportedExchangesIncludesRegistered(t *testing.T) {
	Register("supported-test-1", &stubAdapter{name: "supported-test-1"})
	got := SupportedExchanges()

	found := false
	for _, s := range got {
		if s == "supported-test-1" {
			found = true
			break
		}
	}
	if !found {
		t.Errorf("registered adapter missing from SupportedExchanges: %v", got)
	}
}

// types.go enum validators
func TestSide_IsValid(t *testing.T) {
	if !SideBuy.IsValid() || !SideSell.IsValid() {
		t.Errorf("buy/sell should be valid")
	}
	if Side("hold").IsValid() {
		t.Errorf("'hold' should NOT be valid Side")
	}
}

func TestMarginMode_IsValid(t *testing.T) {
	if !MarginIsolated.IsValid() || !MarginCross.IsValid() {
		t.Errorf("isolated/cross should be valid")
	}
	if MarginMode("portfolio").IsValid() {
		t.Errorf("'portfolio' should NOT be valid")
	}
}

func TestMarketType_IsSpot(t *testing.T) {
	if !MarketSpot.IsSpot() {
		t.Errorf("MarketSpot.IsSpot() must be true")
	}
	if MarketFutures.IsSpot() {
		t.Errorf("MarketFutures.IsSpot() must be false")
	}
	if MarketType("").IsSpot() {
		t.Errorf("default zero-value should NOT be spot (backward-compat = futures)")
	}
}
