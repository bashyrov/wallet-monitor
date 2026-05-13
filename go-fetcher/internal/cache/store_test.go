package cache

import (
	"sync"
	"testing"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

func mkSnap(price, size float64) ws.Snapshot {
	return ws.Snapshot{
		Symbol: "BTC",
		Bids:   []ws.Level{{price - 1, size}},
		Asks:   []ws.Level{{price + 1, size}},
	}
}

func TestStore_StoreThenGet(t *testing.T) {
	s := New()
	s.Store("binance", "BTC", mkSnap(60000, 1.0), "ws")
	e, ok := s.Get("binance", "BTC")
	if !ok {
		t.Fatal("Get miss after Store")
	}
	if len(e.Bids) != 1 || e.Bids[0][0] != 59999 {
		t.Errorf("bids not preserved: %v", e.Bids)
	}
	if e.Source != "ws" {
		t.Errorf("source: %q", e.Source)
	}
}

func TestStore_GetReturnsCopyNotPointer(t *testing.T) {
	s := New()
	s.Store("binance", "BTC", mkSnap(60000, 1), "ws")
	e1, _ := s.Get("binance", "BTC")
	// Caller mutates returned slice — must NOT affect store
	// (Note: Entry returned is a SHALLOW copy; the slices themselves
	// are shared. Per the code docstring "caller must not mutate
	// slices". Mutating the Entry fields IS safe.)
	e1.Source = "rest"
	e2, _ := s.Get("binance", "BTC")
	if e2.Source != "ws" {
		t.Errorf("mutating Entry copy leaked back into store: %q", e2.Source)
	}
}

func TestStore_OverwriteUpdatesBookAndVersion(t *testing.T) {
	s := New()
	s.Store("binance", "BTC", mkSnap(60000, 1), "ws")
	v1 := s.Versions()["binance"]
	s.Store("binance", "BTC", mkSnap(60001, 2), "ws")
	v2 := s.Versions()["binance"]
	if v2 <= v1 {
		t.Errorf("version must advance: %d → %d", v1, v2)
	}
	e, _ := s.Get("binance", "BTC")
	if e.Bids[0][0] != 60000 { // 60001-1
		t.Errorf("bid not updated: %v", e.Bids)
	}
}

func TestStore_VersionsPerExchange(t *testing.T) {
	s := New()
	s.Store("binance", "BTC", mkSnap(60000, 1), "ws")
	s.Store("bybit", "BTC", mkSnap(60000, 1), "ws")
	s.Store("binance", "ETH", mkSnap(3000, 1), "ws")

	versions := s.Versions()
	if versions["binance"] != 2 {
		t.Errorf("binance: want 2 (BTC + ETH), got %d", versions["binance"])
	}
	if versions["bybit"] != 1 {
		t.Errorf("bybit: want 1, got %d", versions["bybit"])
	}
}

func TestStore_GetMissReturnsFalse(t *testing.T) {
	s := New()
	if _, ok := s.Get("binance", "NONE"); ok {
		t.Errorf("Get on absent key should return false")
	}
}

func TestStore_TouchCreatesEmptyEntry(t *testing.T) {
	s := New()
	// Touch before Store — pre-creates entry for WS subscribe path
	s.Touch("binance", "BTC")
	e, ok := s.Get("binance", "BTC")
	if !ok {
		t.Fatal("Touch should create entry")
	}
	if e.LastRequestAt.IsZero() {
		t.Errorf("LastRequestAt should be set by Touch")
	}
}

func TestStore_TouchBumpsLastRequestAtOnExisting(t *testing.T) {
	s := New()
	s.Store("binance", "BTC", mkSnap(60000, 1), "ws")
	before, _ := s.Get("binance", "BTC")
	time.Sleep(10 * time.Millisecond)
	s.Touch("binance", "BTC")
	after, _ := s.Get("binance", "BTC")
	if !after.LastRequestAt.After(before.LastRequestAt) {
		t.Errorf("LastRequestAt did not advance: %v → %v",
			before.LastRequestAt, after.LastRequestAt)
	}
}

func TestStore_PruneIdleEntries(t *testing.T) {
	s := New()
	s.Store("binance", "BTC", mkSnap(60000, 1), "ws")
	s.Store("bybit", "ETH", mkSnap(3000, 1), "ws")
	// Manually backdate bybit:ETH's LastRequestAt
	s.mu.Lock()
	s.books["bybit:ETH"].LastRequestAt = time.Now().Add(-5 * time.Minute)
	s.mu.Unlock()

	removed := s.Prune(1 * time.Minute)
	if removed != 1 {
		t.Errorf("Prune removed %d, want 1", removed)
	}
	if _, ok := s.Get("binance", "BTC"); !ok {
		t.Errorf("fresh BTC was pruned")
	}
	if _, ok := s.Get("bybit", "ETH"); ok {
		t.Errorf("stale ETH still present")
	}
}

func TestStore_EvictStaleByUpdatedAt(t *testing.T) {
	// EvictStale targets venues that stopped pushing deltas — UpdatedAt
	// drifts back even though LastRequestAt is fresh (web role still asks).
	s := New()
	s.Store("binance", "BTC", mkSnap(60000, 1), "ws")
	s.Store("mexc", "DEADCOIN", mkSnap(0.001, 100), "ws")
	// Touch mexc:DEADCOIN — keeps LastRequestAt fresh but UpdatedAt is the
	// original (we'll backdate it below)
	s.Touch("mexc", "DEADCOIN")
	s.mu.Lock()
	s.books["mexc:DEADCOIN"].UpdatedAt = time.Now().Add(-1 * time.Hour)
	s.mu.Unlock()

	removed := s.EvictStale(30 * time.Minute)
	if removed != 1 {
		t.Errorf("EvictStale removed %d, want 1", removed)
	}
	if _, ok := s.Get("mexc", "DEADCOIN"); ok {
		t.Errorf("stale DEADCOIN still present after EvictStale")
	}
	if _, ok := s.Get("binance", "BTC"); !ok {
		t.Errorf("fresh BTC was evicted")
	}
}

func TestStore_SnapshotCopiesAllEntries(t *testing.T) {
	s := New()
	s.Store("binance", "BTC", mkSnap(60000, 1), "ws")
	s.Store("bybit", "ETH", mkSnap(3000, 1), "ws")

	snap := s.Snapshot()
	if len(snap) != 2 {
		t.Errorf("snapshot len: want 2 got %d", len(snap))
	}
	if _, ok := snap["binance:BTC"]; !ok {
		t.Errorf("missing binance:BTC in snapshot")
	}
}

func TestStore_OnUpdateHookFires(t *testing.T) {
	s := New()
	var got struct {
		mu       sync.Mutex
		ex, sym  string
		nbids    int
	}
	s.SetOnUpdate(func(ex, sym string, bids, asks []ws.Level) {
		got.mu.Lock()
		got.ex = ex
		got.sym = sym
		got.nbids = len(bids)
		got.mu.Unlock()
	})
	s.Store("binance", "BTC", mkSnap(60000, 1), "ws")

	got.mu.Lock()
	defer got.mu.Unlock()
	if got.ex != "binance" || got.sym != "BTC" {
		t.Errorf("hook args: ex=%q sym=%q", got.ex, got.sym)
	}
	if got.nbids != 1 {
		t.Errorf("hook bids: %d", got.nbids)
	}
}

func TestStore_OnUpdateNilHookIsNoOp(t *testing.T) {
	s := New()
	// No SetOnUpdate call — hook stays nil. Must not panic.
	s.Store("binance", "BTC", mkSnap(60000, 1), "ws")
}

func TestStore_ConcurrentStoreAndGetSafe(t *testing.T) {
	// Smoke test: race detector enabled (`go test -race`) catches
	// data races. Without race flag this is just a stress smoke.
	s := New()
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		for i := 0; i < 200; i++ {
			s.Store("binance", "BTC", mkSnap(float64(60000+i), 1), "ws")
		}
	}()
	go func() {
		defer wg.Done()
		for i := 0; i < 200; i++ {
			_, _ = s.Get("binance", "BTC")
		}
	}()
	wg.Wait()
}

func TestStore_VersionsReturnsCopy(t *testing.T) {
	s := New()
	s.Store("binance", "BTC", mkSnap(60000, 1), "ws")
	v1 := s.Versions()
	v1["binance"] = 999 // mutate copy
	v2 := s.Versions()
	if v2["binance"] == 999 {
		t.Errorf("Versions returned shared map — mutation leaked")
	}
}
