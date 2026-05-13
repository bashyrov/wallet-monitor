package bootstrap

import (
	"os"
	"path/filepath"
	"testing"
)

func writeFundingJSON(t *testing.T, dir string, contents string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, "funding.json"), []byte(contents), 0o644); err != nil {
		t.Fatalf("write: %v", err)
	}
}

func TestTopSymbols_FromFundingJSON(t *testing.T) {
	dir := t.TempDir()
	// readFromFunding falls back to Default20 if its RESULT < 5 symbols.
	// Provide 6 unique symbols + ask for n=5 so the returned slice has 5.
	writeFundingJSON(t, dir, `{"rows":[
		{"symbol":"BTC","exchange":"binance","volume_usd":1000000000},
		{"symbol":"BTC","exchange":"bybit","volume_usd":500000000},
		{"symbol":"ETH","exchange":"binance","volume_usd":800000000},
		{"symbol":"SOL","exchange":"binance","volume_usd":200000000},
		{"symbol":"DOGE","exchange":"binance","volume_usd":100000000},
		{"symbol":"AVAX","exchange":"binance","volume_usd":50000000}
	]}`)
	got := TopSymbols(dir, 5)
	if len(got) != 5 {
		t.Fatalf("len: want 5 got %d", len(got))
	}
	// Order by max volume desc: BTC > ETH > SOL > DOGE > AVAX
	if got[0] != "BTC" || got[1] != "ETH" || got[2] != "SOL" || got[3] != "DOGE" || got[4] != "AVAX" {
		t.Errorf("order: %v", got)
	}
}

func TestTopSymbols_VolumeMaxAcrossVenues(t *testing.T) {
	// BTC quoted on both venues — should take max, not sum.
	// Need ≥5 unique symbols to bypass the fallback threshold.
	dir := t.TempDir()
	writeFundingJSON(t, dir, `{"rows":[
		{"symbol":"BTC","exchange":"binance","volume_usd":100},
		{"symbol":"BTC","exchange":"bybit","volume_usd":1000},
		{"symbol":"ETH","exchange":"binance","volume_usd":500},
		{"symbol":"SOL","exchange":"binance","volume_usd":50},
		{"symbol":"DOGE","exchange":"binance","volume_usd":25},
		{"symbol":"AVAX","exchange":"binance","volume_usd":12}
	]}`)
	got := TopSymbols(dir, 5)
	// BTC max=1000 should beat ETH max=500
	if len(got) < 2 || got[0] != "BTC" || got[1] != "ETH" {
		t.Errorf("ordering: %v", got)
	}
}

func TestTopSymbols_FallsBackOnMissingFile(t *testing.T) {
	got := TopSymbols(t.TempDir(), 5)
	// Default20 has BTC first
	if len(got) != 5 {
		t.Errorf("len: want 5 got %d", len(got))
	}
	if got[0] != "BTC" {
		t.Errorf("default order: %v", got)
	}
}

func TestTopSymbols_FallsBackOnCorruptJSON(t *testing.T) {
	dir := t.TempDir()
	writeFundingJSON(t, dir, `{garbage`)
	got := TopSymbols(dir, 3)
	if len(got) != 3 {
		t.Errorf("fallback len: %d", len(got))
	}
}

func TestTopSymbols_NLargerThanRankedReturnsAll(t *testing.T) {
	dir := t.TempDir()
	// Exactly 6 ranked symbols (≥5 threshold met). Asking for 100 returns 6.
	writeFundingJSON(t, dir, `{"rows":[
		{"symbol":"BTC","exchange":"binance","volume_usd":1000},
		{"symbol":"ETH","exchange":"binance","volume_usd":500},
		{"symbol":"SOL","exchange":"binance","volume_usd":300},
		{"symbol":"DOGE","exchange":"binance","volume_usd":200},
		{"symbol":"AVAX","exchange":"binance","volume_usd":100},
		{"symbol":"LINK","exchange":"binance","volume_usd":50}
	]}`)
	got := TopSymbols(dir, 100)
	if len(got) != 6 {
		t.Errorf("len: %d (should be 6 — all ranked, no default padding)", len(got))
	}
}

func TestTopSymbols_ZeroVolumeFiltered(t *testing.T) {
	dir := t.TempDir()
	writeFundingJSON(t, dir, `{"rows":[
		{"symbol":"BTC","exchange":"binance","volume_usd":1000},
		{"symbol":"DEADCOIN","exchange":"binance","volume_usd":0}
	]}`)
	got := TopSymbols(dir, 5)
	for _, s := range got {
		if s == "DEADCOIN" {
			t.Errorf("zero-volume symbol leaked through: %v", got)
		}
	}
}

func TestTopSymbols_NLessThanDefaultFallback(t *testing.T) {
	// Sparse file (< 5 rows) → fallback to Default20[:n]
	dir := t.TempDir()
	writeFundingJSON(t, dir, `{"rows":[
		{"symbol":"BTC","exchange":"binance","volume_usd":1000}
	]}`)
	got := TopSymbols(dir, 3)
	// Only 1 symbol in file < 5 threshold → use Default20[:3]
	if len(got) != 3 {
		t.Errorf("len: %d", len(got))
	}
	if got[0] != "BTC" {
		t.Errorf("default[0]: %v", got)
	}
}

func TestDefault20_LengthIs20(t *testing.T) {
	if len(Default20) != 20 {
		t.Errorf("Default20: want 20 got %d", len(Default20))
	}
}
