package symbols

import (
	"testing"
	"time"
)

func TestPrewarmSet_StoresVenueBucket(t *testing.T) {
	m := New()
	m.PrewarmSet("binance", []string{"BTC", "ETH", "SOL"})
	if len(m.prewarm["binance"]) != 3 {
		t.Errorf("binance prewarm size: want 3 got %d", len(m.prewarm["binance"]))
	}
	if _, ok := m.prewarm["binance"]["BTC"]; !ok {
		t.Errorf("BTC missing from binance prewarm")
	}
}

func TestPrewarmSet_ReplacesNotAppends(t *testing.T) {
	m := New()
	m.PrewarmSet("binance", []string{"BTC", "ETH"})
	m.PrewarmSet("binance", []string{"SOL"}) // replaces
	if len(m.prewarm["binance"]) != 1 {
		t.Errorf("replace failed: want 1 entry got %d", len(m.prewarm["binance"]))
	}
	if _, ok := m.prewarm["binance"]["BTC"]; ok {
		t.Errorf("BTC should be gone after replace")
	}
	if _, ok := m.prewarm["binance"]["SOL"]; !ok {
		t.Errorf("SOL missing")
	}
}

func TestPrewarmSet_FiltersEmptyStrings(t *testing.T) {
	m := New()
	m.PrewarmSet("binance", []string{"BTC", "", "ETH", ""})
	if len(m.prewarm["binance"]) != 2 {
		t.Errorf("empty strings should be filtered: %d entries", len(m.prewarm["binance"]))
	}
}

func TestTouch_RecordsTimestamp(t *testing.T) {
	m := New()
	before := time.Now()
	m.Touch("binance", "BTC")
	got := m.userSubs["binance"]["BTC"]
	if got.Before(before) {
		t.Errorf("touch timestamp before call: %v vs %v", got, before)
	}
}

func TestTouch_EmptyArgsIgnored(t *testing.T) {
	m := New()
	m.Touch("", "BTC")
	m.Touch("binance", "")
	if len(m.userSubs) != 0 {
		t.Errorf("empty venue/symbol should not record: %v", m.userSubs)
	}
}

func TestTouch_UpdatesExisting(t *testing.T) {
	m := New()
	m.Touch("binance", "BTC")
	first := m.userSubs["binance"]["BTC"]
	time.Sleep(2 * time.Millisecond)
	m.Touch("binance", "BTC")
	second := m.userSubs["binance"]["BTC"]
	if !second.After(first) {
		t.Errorf("touch should advance timestamp: %v vs %v", first, second)
	}
}

func TestUntouch_RemovesSymbol(t *testing.T) {
	m := New()
	m.Touch("binance", "BTC")
	m.Touch("binance", "ETH")
	m.Untouch("binance", "BTC")
	if _, ok := m.userSubs["binance"]["BTC"]; ok {
		t.Errorf("BTC should be removed")
	}
	if _, ok := m.userSubs["binance"]["ETH"]; !ok {
		t.Errorf("ETH should remain")
	}
}

func TestUntouch_AbsentNoOp(t *testing.T) {
	m := New()
	// Untouch on never-touched — must not panic
	m.Untouch("binance", "BTC")
	m.Untouch("nonexistent", "X")
}

func TestSetsEqual_Empty(t *testing.T) {
	a := map[string]struct{}{}
	b := map[string]struct{}{}
	if !setsEqual(a, b) {
		t.Errorf("two empty sets should be equal")
	}
}

func TestSetsEqual_DifferentLen(t *testing.T) {
	a := map[string]struct{}{"BTC": {}}
	b := map[string]struct{}{}
	if setsEqual(a, b) {
		t.Errorf("different-length sets not equal")
	}
}

func TestSetsEqual_SameElements(t *testing.T) {
	a := map[string]struct{}{"BTC": {}, "ETH": {}}
	b := map[string]struct{}{"ETH": {}, "BTC": {}}
	if !setsEqual(a, b) {
		t.Errorf("same elements (different order) should be equal")
	}
}

func TestSetsEqual_DifferentElements(t *testing.T) {
	a := map[string]struct{}{"BTC": {}, "ETH": {}}
	b := map[string]struct{}{"BTC": {}, "SOL": {}}
	if setsEqual(a, b) {
		t.Errorf("ETH vs SOL — different elements")
	}
}

func TestCopySet_ProducesIndependentMap(t *testing.T) {
	src := map[string]struct{}{"BTC": {}, "ETH": {}}
	cp := copySet(src)
	// mutate copy
	cp["SOL"] = struct{}{}
	delete(cp, "BTC")
	// src untouched
	if _, ok := src["BTC"]; !ok {
		t.Errorf("source mutated: BTC missing")
	}
	if _, ok := src["SOL"]; ok {
		t.Errorf("source mutated: SOL added")
	}
}

func TestIdleWindow_TouchExpiresAfter(t *testing.T) {
	// Test the IdleWindow constant exists and is the documented value.
	if IdleWindow != 120*time.Second {
		t.Errorf("IdleWindow: want 120s got %v", IdleWindow)
	}
}

func TestReconcile_NoOpWithoutRunners(t *testing.T) {
	// Without registered runners reconcile should be no-op (no panic).
	m := New()
	m.PrewarmSet("binance", []string{"BTC", "ETH"})
	m.Touch("binance", "SOL")
	m.reconcile() // venues set is empty → no work
	// `current` should remain empty since no venue registered
	if len(m.current) != 0 {
		t.Errorf("current should be empty without registered runners: %v", m.current)
	}
}
