package wsbroadcast

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestInoutDiffers_BothNil(t *testing.T) {
	if inoutDiffers(nil, nil) {
		t.Errorf("nil == nil should not differ")
	}
}

func TestInoutDiffers_NilVsNonNil(t *testing.T) {
	f := 1.5
	if !inoutDiffers(nil, &f) {
		t.Errorf("nil vs *f should differ")
	}
	if !inoutDiffers(&f, nil) {
		t.Errorf("*f vs nil should differ")
	}
}

func TestInoutDiffers_SameValue(t *testing.T) {
	a, b := 1.5, 1.5
	if inoutDiffers(&a, &b) {
		t.Errorf("equal values should not differ")
	}
}

func TestInoutDiffers_DifferentValue(t *testing.T) {
	a, b := 1.5, 1.6
	if !inoutDiffers(&a, &b) {
		t.Errorf("1.5 vs 1.6 should differ")
	}
}

func TestInoutCloneOpp_ProducesIndependentCopy(t *testing.T) {
	orig := map[string]any{"symbol": "BTC", "rate": 0.0001}
	cp := inoutCloneOpp(orig)
	cp["symbol"] = "ETH"
	if orig["symbol"] != "BTC" {
		t.Errorf("clone mutation leaked: %v", orig)
	}
}

func TestInoutAppendUniq_AddsNew(t *testing.T) {
	got := inoutAppendUniq([]string{"a", "b"}, "c")
	if len(got) != 3 {
		t.Errorf("len: %d", len(got))
	}
}

func TestInoutAppendUniq_DedupesExisting(t *testing.T) {
	got := inoutAppendUniq([]string{"a", "b", "c"}, "b")
	if len(got) != 3 {
		t.Errorf("dedupe: should stay at 3, got %d", len(got))
	}
}

// Helper to write arbitrage files for refreshIndex tests
func writeArbFile(t *testing.T, dir, name string, opps []map[string]any) {
	t.Helper()
	doc := map[string]any{"opportunities": opps}
	b, _ := json.Marshal(doc)
	if err := os.WriteFile(filepath.Join(dir, name), b, 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestRefreshIndex_FuturesBothSides(t *testing.T) {
	dir := t.TempDir()
	writeArbFile(t, dir, "arbitrage.json", []map[string]any{
		{"symbol": "BTC", "long_exchange": "binance", "short_exchange": "bybit", "rate": 0.0001},
	})
	writeArbFile(t, dir, "spot_arbitrage.json", []map[string]any{})
	writeArbFile(t, dir, "dex_arbitrage.json", []map[string]any{})

	p := NewInOutPatcher(nil, NewHub("test"), dir)
	p.refreshIndex()

	// Both binance:BTC and bybit:BTC should index the pair
	if got := p.affected["binance:BTC"]; len(got) != 1 {
		t.Errorf("binance:BTC affected: %v", got)
	}
	if got := p.affected["bybit:BTC"]; len(got) != 1 {
		t.Errorf("bybit:BTC affected: %v", got)
	}
	key := "BTC|binance|bybit"
	if _, ok := p.pairs[key]; !ok {
		t.Errorf("pair key missing: %v", keysOfPairs(p.pairs))
	}
	if p.pairs[key]["_mode"] != "futures" {
		t.Errorf("mode: %v", p.pairs[key]["_mode"])
	}
}

func TestRefreshIndex_SpotUsesSpotSuffix(t *testing.T) {
	dir := t.TempDir()
	writeArbFile(t, dir, "arbitrage.json", []map[string]any{})
	writeArbFile(t, dir, "spot_arbitrage.json", []map[string]any{
		{"symbol": "ETH", "spot_exchange": "binance", "short_exchange": "bybit"},
	})
	writeArbFile(t, dir, "dex_arbitrage.json", []map[string]any{})

	p := NewInOutPatcher(nil, NewHub("test"), dir)
	p.refreshIndex()

	// Spot leg's OB store key has _spot suffix
	if got := p.affected["binance_spot:ETH"]; len(got) != 1 {
		t.Errorf("binance_spot:ETH should be indexed, got %v", got)
	}
	if got := p.affected["bybit:ETH"]; len(got) != 1 {
		t.Errorf("bybit:ETH should be indexed, got %v", got)
	}
}

func TestRefreshIndex_DEXOnlyShortSideIndexed(t *testing.T) {
	dir := t.TempDir()
	writeArbFile(t, dir, "arbitrage.json", []map[string]any{})
	writeArbFile(t, dir, "spot_arbitrage.json", []map[string]any{})
	writeArbFile(t, dir, "dex_arbitrage.json", []map[string]any{
		{"symbol": "SOL", "dex_name": "uniswap", "short_exchange": "binance"},
	})

	p := NewInOutPatcher(nil, NewHub("test"), dir)
	p.refreshIndex()

	// Only the perp short leg has an orderbook
	if got := p.affected["binance:SOL"]; len(got) != 1 {
		t.Errorf("binance:SOL should be indexed, got %v", got)
	}
	if got := p.affected["uniswap:SOL"]; len(got) != 0 {
		t.Errorf("DEX side should NOT be indexed (no OB), got %v", got)
	}
}

func TestRefreshIndex_MissingFieldsSkipped(t *testing.T) {
	dir := t.TempDir()
	writeArbFile(t, dir, "arbitrage.json", []map[string]any{
		{"symbol": "BTC", "long_exchange": ""}, // missing short
		{"symbol": "ETH", "short_exchange": "bybit"}, // missing long
		{"long_exchange": "binance", "short_exchange": "bybit"}, // missing symbol
	})
	writeArbFile(t, dir, "spot_arbitrage.json", []map[string]any{})
	writeArbFile(t, dir, "dex_arbitrage.json", []map[string]any{})

	p := NewInOutPatcher(nil, NewHub("test"), dir)
	p.refreshIndex()

	if len(p.pairs) != 0 {
		t.Errorf("malformed opps should be skipped, got %v", p.pairs)
	}
}

func TestRefreshIndex_HandlesMissingFiles(t *testing.T) {
	dir := t.TempDir()
	// No files written — refreshIndex must not panic
	p := NewInOutPatcher(nil, NewHub("test"), dir)
	p.refreshIndex()
	if len(p.pairs) != 0 {
		t.Errorf("no files → empty index, got %v", p.pairs)
	}
}

func TestReadOpps_CorruptJSONReturnsNil(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "arb.json")
	_ = os.WriteFile(path, []byte("{nope"), 0o644)
	got := readOpps(path)
	if got != nil {
		t.Errorf("corrupt JSON should return nil, got %v", got)
	}
}

func keysOfPairs(m map[string]map[string]any) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}
