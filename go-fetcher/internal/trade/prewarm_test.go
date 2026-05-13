package trade

import (
	"strings"
	"testing"
	"time"
)

func TestVenueHostnames_NotEmpty(t *testing.T) {
	if len(venueHostnames) == 0 {
		t.Errorf("venueHostnames should not be empty")
	}
}

func TestVenueHostnames_NoEmptyStrings(t *testing.T) {
	for i, h := range venueHostnames {
		if strings.TrimSpace(h) == "" {
			t.Errorf("venueHostnames[%d] is empty", i)
		}
	}
}

func TestVenueHostnames_HaveExpectedCoreVenues(t *testing.T) {
	// CLAUDE.md lists 18 venues; smoke-check the headline ones are present.
	required := []string{
		"fapi.binance.com",
		"api.bybit.com",
		"www.okx.com",
		"api.hyperliquid.xyz",
		"api.gateio.ws",
	}
	set := make(map[string]struct{}, len(venueHostnames))
	for _, h := range venueHostnames {
		set[h] = struct{}{}
	}
	for _, r := range required {
		if _, ok := set[r]; !ok {
			t.Errorf("required venue hostname missing: %s", r)
		}
	}
}

func TestVenueHostnames_NoDuplicates(t *testing.T) {
	seen := make(map[string]int, len(venueHostnames))
	for _, h := range venueHostnames {
		seen[h]++
	}
	for h, n := range seen {
		if n > 1 {
			t.Errorf("duplicate hostname: %s (×%d)", h, n)
		}
	}
}

func TestPrewarmDNS_DoesNotBlock(t *testing.T) {
	// PrewarmDNS spawns goroutines and returns immediately. Should not
	// block even with all hostnames in the live list — each goroutine
	// owns its own ctx.
	done := make(chan struct{})
	go func() {
		PrewarmDNS()
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Errorf("PrewarmDNS blocked the caller")
	}
}

func TestPrewarmDNS_GoroutineCleanup(t *testing.T) {
	// Run twice — verify second call doesn't accumulate or deadlock on
	// shared state. Hostnames are pure-read, so this is just a smoke check.
	PrewarmDNS()
	PrewarmDNS()
	// Sleep briefly so goroutines from both calls overlap a bit.
	time.Sleep(50 * time.Millisecond)
	// Test passes if no panic / deadlock.
}
