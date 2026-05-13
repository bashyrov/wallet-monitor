package arb

import (
	"testing"
)

// spot.go helpers
func TestSpotFeeOf_KnownVenue(t *testing.T) {
	cases := map[string]float64{
		"binance": 0.001,
		"mexc":    0.0005,
		"htx":     0.002,
	}
	for ex, want := range cases {
		got := spotFeeOf(ex)
		if got != want {
			t.Errorf("spotFeeOf(%q): want %v got %v", ex, want, got)
		}
	}
}

func TestSpotFeeOf_UnknownReturnsDefault(t *testing.T) {
	if got := spotFeeOf("nonexistent"); got != 0.001 {
		t.Errorf("unknown spot fee: want 0.001 got %v", got)
	}
}

func TestParseFloat_Empty(t *testing.T) {
	if v := parseFloat(""); v != 0 {
		t.Errorf("empty string: want 0 got %v", v)
	}
}

func TestParseFloat_ValidNumber(t *testing.T) {
	if v := parseFloat("42.5"); v != 42.5 {
		t.Errorf("valid: want 42.5 got %v", v)
	}
	if v := parseFloat("-3.14"); v != -3.14 {
		t.Errorf("negative: want -3.14 got %v", v)
	}
}

func TestParseFloat_GarbageReturnsZero(t *testing.T) {
	if v := parseFloat("not a number"); v != 0 {
		t.Errorf("garbage: want 0 got %v", v)
	}
}

// dex.go helpers — pickFromPools logic
func mkPair(chain, base, baseAddr, quote, priceUSD string, liqUSD, volH24 float64) dsPair {
	p := dsPair{
		ChainID:  chain,
		DexID:    "uniswap",
		PriceUSD: priceUSD,
	}
	p.BaseToken.Symbol = base
	p.BaseToken.Address = baseAddr
	p.QuoteToken.Symbol = quote
	p.Liquidity.USD = liqUSD
	p.Volume.H24 = volH24
	return p
}

func TestPickFromPools_RejectsNonAcceptedQuote(t *testing.T) {
	// Quote token DAI not in acceptedQuotes — should bail with "quote"
	pairs := []dsPair{
		mkPair("ethereum", "BTC", "0xabc", "DAI", "60000", 1e6, 1e6),
	}
	info, reason := pickFromPools("ethereum", "0xabc", pairs)
	if info != nil {
		t.Errorf("non-USDT/USDC quote should reject")
	}
	if reason != "quote" {
		t.Errorf("reason: want 'quote' got %q", reason)
	}
}

func TestPickFromPools_RejectsBelowLiquidityFloor(t *testing.T) {
	// Liquidity below minDEXLiqUSD (5000) — "liq"
	pairs := []dsPair{
		mkPair("ethereum", "BTC", "0xabc", "USDT", "60000", 1000, 1e6),
	}
	info, reason := pickFromPools("ethereum", "0xabc", pairs)
	if info != nil {
		t.Errorf("low liquidity should reject")
	}
	if reason != "liq" {
		t.Errorf("reason: want 'liq' got %q", reason)
	}
}

func TestPickFromPools_RejectsBelowVolumeFloor(t *testing.T) {
	// Volume below minDEXVolUSD (1000) — "liq" (single reason for both filters)
	pairs := []dsPair{
		mkPair("ethereum", "BTC", "0xabc", "USDT", "60000", 1e6, 100),
	}
	info, reason := pickFromPools("ethereum", "0xabc", pairs)
	if info != nil {
		t.Errorf("low volume should reject")
	}
	if reason != "liq" {
		t.Errorf("reason: want 'liq' got %q", reason)
	}
}

func TestPickFromPools_HappyPath(t *testing.T) {
	pairs := []dsPair{
		mkPair("ethereum", "BTC", "0xabc", "USDT", "60000", 100_000, 50_000),
	}
	info, reason := pickFromPools("ethereum", "0xabc", pairs)
	if info == nil {
		t.Fatalf("happy path nil: reason=%q", reason)
	}
	if info.Symbol != "BTC" {
		t.Errorf("symbol: %v", info.Symbol)
	}
	if info.Price != 60000 {
		t.Errorf("price: %v", info.Price)
	}
	if info.LiquidityUSD != 100_000 {
		t.Errorf("liquidity: %v", info.LiquidityUSD)
	}
	if reason != "ok" {
		t.Errorf("reason: %q", reason)
	}
}

func TestPickFromPools_FiltersByChain(t *testing.T) {
	// Caller asks for ethereum but pairs contain solana — defence layer
	pairs := []dsPair{
		mkPair("solana", "BTC", "0xabc", "USDT", "60000", 1e6, 1e6),
	}
	info, reason := pickFromPools("ethereum", "0xabc", pairs)
	if info != nil {
		t.Errorf("wrong chain should filter out, got %+v", info)
	}
	// No matching pools at all → "quote" (we never saw any matching the
	// chain/address, so anyQuote stays false)
	if reason != "quote" {
		t.Errorf("reason: want 'quote' got %q", reason)
	}
}

func TestPickFromPools_FiltersByAddressLowercase(t *testing.T) {
	// Caller passes uppercase address; pool has lowercase. Should match.
	pairs := []dsPair{
		mkPair("ethereum", "BTC", "0xabc", "USDT", "60000", 100_000, 50_000),
	}
	info, _ := pickFromPools("ethereum", "0xABC", pairs)
	if info == nil {
		t.Errorf("case-insensitive address match failed")
	}
}

func TestPickFromPools_ConsensusRejectsOutlier(t *testing.T) {
	// 5 pools — 4 around $60000, 1 wild outlier ($100000). Outlier should
	// fail the consensus check (1.5% max dev from median).
	pairs := []dsPair{
		mkPair("ethereum", "BTC", "0xabc", "USDT", "60000", 100_000, 50_000),
		mkPair("ethereum", "BTC", "0xabc", "USDT", "60100", 90_000, 40_000),
		mkPair("ethereum", "BTC", "0xabc", "USDT", "59900", 80_000, 30_000),
		mkPair("ethereum", "BTC", "0xabc", "USDT", "60050", 70_000, 25_000),
		mkPair("ethereum", "BTC", "0xabc", "USDT", "100000", 200_000, 100_000), // outlier with highest liq
	}
	info, reason := pickFromPools("ethereum", "0xabc", pairs)
	// Sorted by liq desc — outlier first; but consensus check should
	// skip it since it deviates >1.5% from median (~60050).
	if info == nil {
		t.Fatalf("expected consensus winner, got nil (reason=%q)", reason)
	}
	if info.Price == 100000 {
		t.Errorf("outlier should be rejected, got it as winner")
	}
}
