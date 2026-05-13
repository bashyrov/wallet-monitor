package wsbroadcast

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func writeFundingFile(t *testing.T, dir string, rows []map[string]any) {
	t.Helper()
	doc := map[string]any{
		"ts":        1718000001.0,
		"rows":      rows,
		"exchanges": []any{},
	}
	b, _ := json.Marshal(doc)
	if err := os.WriteFile(filepath.Join(dir, "funding.json"), b, 0o644); err != nil {
		t.Fatalf("write: %v", err)
	}
}

func TestFundingKey_Format(t *testing.T) {
	k := fundingKey(map[string]any{"exchange": "binance", "symbol": "BTC"})
	if k != "binance|BTC" {
		t.Errorf("funding key: want binance|BTC got %q", k)
	}
}

func TestFundingKey_EmptyFieldYieldsEmpty(t *testing.T) {
	if fundingKey(map[string]any{"symbol": "BTC"}) != "" {
		t.Errorf("missing exchange should yield empty key")
	}
	if fundingKey(map[string]any{"exchange": "binance"}) != "" {
		t.Errorf("missing symbol should yield empty key")
	}
}

func TestSplitFundingKey_RoundTrip(t *testing.T) {
	got := splitFundingKey("binance|BTC")
	if len(got) != 2 {
		t.Fatalf("len: want 2 got %d", len(got))
	}
	if got[0] != "binance" || got[1] != "BTC" {
		t.Errorf("split: %v", got)
	}
}

func TestFundingDiffers_OnRateChange(t *testing.T) {
	a := map[string]any{"rate": 0.0001, "price": 60000.0}
	b := map[string]any{"rate": 0.0002, "price": 60000.0}
	if !fundingDiffers(a, b) {
		t.Errorf("rate change should differ")
	}
}

func TestFundingDiffers_OnPriceOnly(t *testing.T) {
	a := map[string]any{"rate": 0.0001, "price": 60000.0}
	b := map[string]any{"rate": 0.0001, "price": 60100.0}
	if !fundingDiffers(a, b) {
		t.Errorf("price change should differ")
	}
}

func TestFundingDiffers_OnAprOnly(t *testing.T) {
	a := map[string]any{"apr": 10.0}
	b := map[string]any{"apr": 10.5}
	if !fundingDiffers(a, b) {
		t.Errorf("apr change should differ")
	}
}

func TestFundingDiffers_OnTsOnlyShouldNotDiffer(t *testing.T) {
	// `ts` is intentionally NOT a diff field — Python's matching set.
	a := map[string]any{"rate": 0.0001, "ts": 1.0}
	b := map[string]any{"rate": 0.0001, "ts": 2.0}
	if fundingDiffers(a, b) {
		t.Errorf("ts-only change must NOT differ")
	}
}

func TestFundingDiffers_OnNonTrackedField(t *testing.T) {
	// `cross_listed` and other flags are not in the diff field set.
	a := map[string]any{"rate": 0.0001, "cross_listed": true}
	b := map[string]any{"rate": 0.0001, "cross_listed": false}
	if fundingDiffers(a, b) {
		t.Errorf("non-tracked field change must NOT differ")
	}
}

func TestReadFundingFile_ValidJSON(t *testing.T) {
	dir := t.TempDir()
	writeFundingFile(t, dir, []map[string]any{
		{"exchange": "binance", "symbol": "BTC", "rate": 0.0001, "price": 60000.0, "apr": 10.95},
	})
	f := NewFunding(dir)
	doc := f.readFundingFile()
	if doc == nil {
		t.Fatal("readFundingFile returned nil for valid file")
	}
	rows := doc["rows"].([]any)
	if len(rows) != 1 {
		t.Errorf("rows: want 1 got %d", len(rows))
	}
}

func TestReadFundingFile_MissingFileReturnsNil(t *testing.T) {
	f := NewFunding(t.TempDir())
	if doc := f.readFundingFile(); doc != nil {
		t.Errorf("missing file should return nil, got %v", doc)
	}
}

func TestReadFundingFile_CorruptJSONReturnsNil(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "funding.json"), []byte("{nope"), 0o644); err != nil {
		t.Fatal(err)
	}
	f := NewFunding(dir)
	if doc := f.readFundingFile(); doc != nil {
		t.Errorf("corrupt JSON should return nil, got %v", doc)
	}
}

func TestSnapshotForNewClient_PopulatesAndCaches(t *testing.T) {
	dir := t.TempDir()
	writeFundingFile(t, dir, []map[string]any{
		{"exchange": "binance", "symbol": "BTC", "rate": 0.0001},
	})
	f := NewFunding(dir)
	snap := f.SnapshotForNewClient()
	if len(snap) == 0 {
		t.Fatal("snapshot empty")
	}
	// Decode and verify shape
	var decoded map[string]any
	if err := json.Unmarshal(snap, &decoded); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if decoded["type"] != "snapshot" {
		t.Errorf("type: %v", decoded["type"])
	}
	rows := decoded["rows"].([]any)
	if len(rows) != 1 {
		t.Errorf("rows: %d", len(rows))
	}
}
