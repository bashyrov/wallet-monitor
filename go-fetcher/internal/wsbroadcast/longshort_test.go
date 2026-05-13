package wsbroadcast

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func writeArbFile(t *testing.T, dir string, opps []map[string]any, ts int64) string {
	t.Helper()
	doc := map[string]any{
		"ts":            ts,
		"fees":          map[string]any{},
		"exchanges":     []any{},
		"opportunities": opps,
	}
	b, _ := json.Marshal(doc)
	path := filepath.Join(dir, "arbitrage.json")
	if err := os.WriteFile(path, b, 0o644); err != nil {
		t.Fatalf("write: %v", err)
	}
	return path
}

func TestReadArbFile_MtimeSkipOnUnchanged(t *testing.T) {
	dir := t.TempDir()
	writeArbFile(t, dir, []map[string]any{
		{"symbol": "BTC", "long_exchange": "binance", "short_exchange": "bybit", "net_apr": 12.5},
	}, 1)
	ls := NewLongShort(dir)

	// First read — should succeed.
	doc := ls.readArbFile()
	if doc == nil {
		t.Fatal("first read returned nil")
	}
	opps := doc["opportunities"].([]any)
	if len(opps) != 1 {
		t.Fatalf("opps: want 1 got %d", len(opps))
	}

	// Second read with unchanged file — mtime cache short-circuits decode.
	doc2 := ls.readArbFile()
	if doc2 != nil {
		t.Errorf("unchanged file should return nil (mtime-skip), got %v", doc2)
	}
}

func TestReadArbFile_RescanAfterMtimeBump(t *testing.T) {
	dir := t.TempDir()
	path := writeArbFile(t, dir, []map[string]any{
		{"symbol": "BTC", "long_exchange": "binance", "short_exchange": "bybit", "net_apr": 12.5},
	}, 1)
	ls := NewLongShort(dir)
	_ = ls.readArbFile() // prime mtime

	// Bump mtime forward so the next stat sees a fresh file.
	future := time.Now().Add(2 * time.Second)
	if err := os.Chtimes(path, future, future); err != nil {
		t.Fatalf("chtimes: %v", err)
	}

	doc := ls.readArbFile()
	if doc == nil {
		t.Fatal("post-mtime-bump read returned nil (should decode)")
	}
}

func TestReadArbFile_MissingFileReturnsNil(t *testing.T) {
	ls := NewLongShort(t.TempDir())
	if doc := ls.readArbFile(); doc != nil {
		t.Errorf("missing file should return nil, got %v", doc)
	}
}

func TestReadArbFile_CorruptJSONReturnsNil(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "arbitrage.json"), []byte("{not json"), 0o644); err != nil {
		t.Fatalf("write: %v", err)
	}
	ls := NewLongShort(dir)
	if doc := ls.readArbFile(); doc != nil {
		t.Errorf("corrupt json should return nil, got %v", doc)
	}
}

func TestForceReadArbFile_BypassesMtimeCache(t *testing.T) {
	dir := t.TempDir()
	writeArbFile(t, dir, []map[string]any{{"symbol": "BTC"}}, 1)
	ls := NewLongShort(dir)
	_ = ls.readArbFile() // establish mtime baseline

	// readArbFile would skip; forceReadArbFile must NOT.
	doc := ls.forceReadArbFile()
	if doc == nil {
		t.Fatal("forceReadArbFile returned nil despite valid file")
	}
}

func TestSnapshotForNewClient_ColdStartCachesResult(t *testing.T) {
	dir := t.TempDir()
	writeArbFile(t, dir, []map[string]any{{"symbol": "BTC", "long_exchange": "binance", "short_exchange": "bybit"}}, 1)
	ls := NewLongShort(dir)

	// First call: cold path — reads file, caches result.
	snap := ls.SnapshotForNewClient()
	if len(snap) == 0 {
		t.Fatal("first snapshot empty")
	}

	// Second call: should hit the cache, NOT re-read the file. To verify,
	// delete the file: cached path should still return the snapshot.
	_ = os.Remove(filepath.Join(dir, "arbitrage.json"))
	snap2 := ls.SnapshotForNewClient()
	if len(snap2) == 0 {
		t.Errorf("post-cache snapshot empty (cache should have kept it)")
	}
}

func TestArbKey_Format(t *testing.T) {
	k := arbKey(map[string]any{
		"symbol":         "BTC",
		"long_exchange":  "binance",
		"short_exchange": "bybit",
	})
	if k != "BTC|binance|bybit" {
		t.Errorf("arbKey: want BTC|binance|bybit got %q", k)
	}
}

func TestArbKey_EmptyFieldYieldsEmptyKey(t *testing.T) {
	k := arbKey(map[string]any{
		"symbol":        "BTC",
		"long_exchange": "binance",
		// short_exchange missing
	})
	if k != "" {
		t.Errorf("missing field should produce empty key, got %q", k)
	}
}

func TestSplitArbKey_Roundtrip(t *testing.T) {
	parts := splitArbKey("BTC|binance|bybit")
	if len(parts) != 3 {
		t.Fatalf("parts: want 3 got %d", len(parts))
	}
	if parts[0] != "BTC" || parts[1] != "binance" || parts[2] != "bybit" {
		t.Errorf("split: %v", parts)
	}
}

func TestOppDiffers_OnPriceChange(t *testing.T) {
	a := map[string]any{"long_price": 60000.0, "short_price": 60010.0}
	b := map[string]any{"long_price": 60001.0, "short_price": 60010.0}
	if !oppDiffers(a, b) {
		t.Errorf("price change should differ")
	}
}

func TestOppDiffers_OnTsOnlyIsSame(t *testing.T) {
	// `ts` is intentionally NOT in the comparison field set — opps that
	// only differ in last-update timestamp must NOT trigger a diff push.
	a := map[string]any{"long_price": 60000.0, "ts": 1.0}
	b := map[string]any{"long_price": 60000.0, "ts": 2.0}
	if oppDiffers(a, b) {
		t.Errorf("ts-only change must NOT differ (no client-visible delta)")
	}
}
