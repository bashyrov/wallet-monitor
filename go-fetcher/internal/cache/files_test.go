package cache

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

func seedStore(s *Store, ex, sym string, price, size float64) {
	s.Store(ex, sym, ws.Snapshot{
		Symbol: sym,
		Bids:   []ws.Level{{price - 1, size}},
		Asks:   []ws.Level{{price + 1, size}},
	}, "ws")
}

func TestDumper_WritesPerExchangeFile(t *testing.T) {
	dir := t.TempDir()
	store := New()
	seedStore(store, "binance", "BTC", 60000, 1)
	seedStore(store, "bybit", "ETH", 3000, 1)

	d := NewDumper(store, dir, 100*time.Millisecond)
	// Force a manual dump cycle without Run()
	if err := d.dump(); err != nil {
		t.Fatalf("dump: %v", err)
	}

	// Per-exchange files
	for _, ex := range []string{"binance", "bybit"} {
		path := filepath.Join(dir, "books."+ex+".json")
		if _, err := os.Stat(path); err != nil {
			t.Errorf("books.%s.json not written: %v", ex, err)
		}
	}
}

func TestDumper_AtomicWrite_NoPartialFile(t *testing.T) {
	dir := t.TempDir()
	store := New()
	seedStore(store, "binance", "BTC", 60000, 1)
	d := NewDumper(store, dir, 100*time.Millisecond)
	if err := d.dump(); err != nil {
		t.Fatalf("dump: %v", err)
	}

	// Verify file is valid JSON (atomic rename guarantees this — no
	// partial-write corruption)
	raw, err := os.ReadFile(filepath.Join(dir, "books.binance.json"))
	if err != nil {
		t.Fatal(err)
	}
	var decoded map[string]any
	if err := json.Unmarshal(raw, &decoded); err != nil {
		t.Errorf("partial/corrupt JSON: %v\n%s", err, raw)
	}
	entry, ok := decoded["binance:BTC"].(map[string]any)
	if !ok {
		t.Fatalf("binance:BTC key missing: %v", decoded)
	}
	if entry["source"] != "ws" {
		t.Errorf("source: %v", entry["source"])
	}
}

func TestDumper_SkipsUnchangedVenue(t *testing.T) {
	dir := t.TempDir()
	store := New()
	seedStore(store, "binance", "BTC", 60000, 1)
	d := NewDumper(store, dir, 100*time.Millisecond)
	if err := d.dump(); err != nil {
		t.Fatal(err)
	}

	// Record mtime of binance file
	path := filepath.Join(dir, "books.binance.json")
	info1, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}

	// Make file mtime artificially old so we'd notice a rewrite
	old := time.Now().Add(-1 * time.Hour)
	_ = os.Chtimes(path, old, old)
	info1, _ = os.Stat(path)

	// Second dump WITHOUT any new Store calls — version hasn't moved,
	// so writeAtomic should skip this venue.
	if err := d.dump(); err != nil {
		t.Fatal(err)
	}

	info2, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if !info2.ModTime().Equal(info1.ModTime()) {
		t.Errorf("file rewritten unnecessarily: %v → %v", info1.ModTime(), info2.ModTime())
	}
}

func TestDumper_RewritesAfterStoreCall(t *testing.T) {
	dir := t.TempDir()
	store := New()
	seedStore(store, "binance", "BTC", 60000, 1)
	d := NewDumper(store, dir, 100*time.Millisecond)
	_ = d.dump()
	path := filepath.Join(dir, "books.binance.json")
	old := time.Now().Add(-1 * time.Hour)
	_ = os.Chtimes(path, old, old)
	info1, _ := os.Stat(path)

	// Updated store → version advances → next dump rewrites
	seedStore(store, "binance", "BTC", 60001, 2)
	if err := d.dump(); err != nil {
		t.Fatal(err)
	}
	info2, _ := os.Stat(path)
	if info2.ModTime().Equal(info1.ModTime()) {
		t.Errorf("file should have been rewritten after Store")
	}
}

func TestDumper_FullMergeThrottled(t *testing.T) {
	dir := t.TempDir()
	store := New()
	seedStore(store, "binance", "BTC", 60000, 1)
	d := NewDumper(store, dir, 100*time.Millisecond)
	// Set throttle to a long interval so the test doesn't race
	d.fullMergeInterval = 10 * time.Second

	if err := d.dump(); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "books.json")
	info1, err := os.Stat(path)
	if err != nil {
		t.Fatalf("books.json missing: %v", err)
	}

	// Backdate mtime; do another dump immediately — throttle should prevent rewrite
	old := time.Now().Add(-1 * time.Hour)
	_ = os.Chtimes(path, old, old)
	seedStore(store, "binance", "ETH", 3000, 1) // bumps per-ex version too
	if err := d.dump(); err != nil {
		t.Fatal(err)
	}
	info2, _ := os.Stat(path)
	// books.json mtime should still be backdated (no rewrite within throttle)
	_ = info1
	if !info2.ModTime().Before(time.Now().Add(-30 * time.Minute)) {
		t.Errorf("books.json rewritten despite throttle: mtime=%v", info2.ModTime())
	}
}

func TestDumper_MasterFileIncludesSpotAndExotics(t *testing.T) {
	dir := t.TempDir()
	store := New()
	seedStore(store, "binance_spot", "BTC", 60000, 1)
	seedStore(store, "paradex", "ETH", 3000, 1)
	seedStore(store, "binance", "SOL", 150, 1) // perp — NOT in master

	d := NewDumper(store, dir, 100*time.Millisecond)
	d.fullMergeInterval = 1 * time.Millisecond // allow immediate full-merge
	if err := d.dump(); err != nil {
		t.Fatal(err)
	}

	raw, err := os.ReadFile(filepath.Join(dir, "books.master.json"))
	if err != nil {
		t.Fatalf("master file: %v", err)
	}
	var decoded map[string]any
	_ = json.Unmarshal(raw, &decoded)

	if _, ok := decoded["binance_spot:BTC"]; !ok {
		t.Errorf("spot key missing from master: %v", keysOf(decoded))
	}
	if _, ok := decoded["paradex:ETH"]; !ok {
		t.Errorf("paradex key missing from master: %v", keysOf(decoded))
	}
	if _, ok := decoded["binance:SOL"]; ok {
		t.Errorf("perp leaked into master: %v", keysOf(decoded))
	}
}

func TestEntryToJSON_FieldShape(t *testing.T) {
	e := Entry{
		Bids:          []ws.Level{{60000, 1.5}},
		Asks:          []ws.Level{{60100, 2.0}},
		UpdatedAt:     time.Unix(1718000001, 0),
		LastRequestAt: time.Unix(1718000002, 0),
		Source:        "ws",
	}
	out := entryToJSON(e)
	if out["source"] != "ws" {
		t.Errorf("source: %v", out["source"])
	}
	if out["ts"] != int64(1718000001000) {
		t.Errorf("ts (ms): %v", out["ts"])
	}
	if out["last_request"] != int64(1718000002000) {
		t.Errorf("last_request (ms): %v", out["last_request"])
	}
}

func TestWriteAtomic_ProducesValidJSONOnFreshFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "test.json")
	v := map[string]any{"a": 1, "b": "hello"}
	if err := writeAtomic(path, v); err != nil {
		t.Fatalf("writeAtomic: %v", err)
	}
	raw, _ := os.ReadFile(path)
	var got map[string]any
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Errorf("invalid JSON: %v", err)
	}
}

func TestWriteAtomic_OverwritesExisting(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "test.json")
	if err := writeAtomic(path, map[string]any{"v": 1}); err != nil {
		t.Fatal(err)
	}
	if err := writeAtomic(path, map[string]any{"v": 2}); err != nil {
		t.Fatal(err)
	}
	raw, _ := os.ReadFile(path)
	var got map[string]any
	_ = json.Unmarshal(raw, &got)
	if got["v"] != float64(2) {
		t.Errorf("overwrite: %v", got)
	}
}

func TestIndexColon(t *testing.T) {
	cases := []struct {
		in   string
		want int
	}{
		{"binance:BTC", 7},
		{"a:b", 1},
		{":x", 0},
		{"nocolon", -1},
		{"", -1},
	}
	for _, c := range cases {
		got := indexColon(c.in)
		if got != c.want {
			t.Errorf("indexColon(%q): want %d got %d", c.in, c.want, got)
		}
	}
}

func keysOf(m map[string]any) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}
