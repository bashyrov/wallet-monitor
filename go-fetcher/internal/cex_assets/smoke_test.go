package cex_assets

import (
	"context"
	"os"
	"strings"
	"testing"
	"time"
)

// These tests hit live venue APIs. Skipped unless CEX_ASSETS_LIVE=1 is
// set so CI/dev builds don't fail on network blips or venue maintenance
// windows. Run explicitly with:
//
//   CEX_ASSETS_LIVE=1 go test ./internal/cex_assets/... -v -run TestLive
//
// Purpose: verify each adapter (a) parses without error and (b) finds
// the well-known canary token USDT on at least one major chain. The
// canary catches schema drift (e.g. a venue renaming "contractAddress"
// → "contract_address") that would silently make every match fail.
func TestLive_Gate(t *testing.T) {
	if os.Getenv("CEX_ASSETS_LIVE") != "1" {
		t.Skip("set CEX_ASSETS_LIVE=1 to run live adapter tests")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	assets, err := FetchGate(ctx, nil)
	if err != nil {
		t.Fatalf("gate: %v", err)
	}
	checkCanary(t, "gate", assets, "USDT", "ethereum", "0xdac17f958d2ee523a2206206994597c13d831ec7")
}

func TestLive_KuCoin(t *testing.T) {
	if os.Getenv("CEX_ASSETS_LIVE") != "1" {
		t.Skip("set CEX_ASSETS_LIVE=1 to run live adapter tests")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	assets, err := FetchKuCoin(ctx, nil)
	if err != nil {
		t.Fatalf("kucoin: %v", err)
	}
	checkCanary(t, "kucoin", assets, "USDT", "ethereum", "0xdac17f958d2ee523a2206206994597c13d831ec7")
}

func TestLive_Bitget(t *testing.T) {
	if os.Getenv("CEX_ASSETS_LIVE") != "1" {
		t.Skip("set CEX_ASSETS_LIVE=1 to run live adapter tests")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	assets, err := FetchBitget(ctx, nil)
	if err != nil {
		t.Fatalf("bitget: %v", err)
	}
	checkCanary(t, "bitget", assets, "USDT", "ethereum", "0xdac17f958d2ee523a2206206994597c13d831ec7")
}

// checkCanary asserts a venue's assets map contains an entry for ticker
// on the given chain with the given address. Logs hits/misses with
// adjacent context so a schema drift is immediately diagnosable.
func checkCanary(t *testing.T, venue string, assets VenueAssets, ticker, chain, address string) {
	t.Helper()
	if len(assets) < 100 {
		t.Fatalf("%s returned only %d tickers — suspicious, expected ≥100", venue, len(assets))
	}
	entries, ok := assets[ticker]
	if !ok {
		// Log neighbours so we can see if the venue is using lowercase / different naming
		hint := ""
		for k := range assets {
			if strings.Contains(strings.ToLower(k), strings.ToLower(ticker)) {
				hint = k
				break
			}
		}
		t.Fatalf("%s: ticker %s missing (hint: similar key=%q)", venue, ticker, hint)
	}
	for _, e := range entries {
		if e.Chain == chain && strings.EqualFold(e.Address, address) {
			t.Logf("%s: %s on %s matched %s ✓ (tickers total=%d)", venue, ticker, chain, address, len(assets))
			return
		}
	}
	t.Fatalf("%s: %s on %s did NOT match expected address %s. Got chains=%+v", venue, ticker, chain, address, entries)
}

// TestRegistryMatchByAddress exercises the read-path in isolation — no
// network. Verifies the Match policy: verified iff chain AND address
// both match.
func TestRegistryMatchByAddress(t *testing.T) {
	r := NewRegistry(t.TempDir())
	r.SetVenue("gate", VenueAssets{
		"USDT": {
			{Chain: "ethereum", Address: "0xdac17f958d2ee523a2206206994597c13d831ec7"},
			{Chain: "bsc", Address: "0x55d398326f99059ff775485246999027b3197955"},
		},
	})
	// Exact match
	res := r.MatchByAddress("gate", "USDT", "ethereum", "0xdAC17F958D2ee523a2206206994597C13D831ec7")
	if !res.Verified {
		t.Fatalf("expected Verified=true on exact match (with case-insensitive address): %+v", res)
	}
	if res.MatchChain != "ethereum" {
		t.Errorf("MatchChain=%s; want ethereum", res.MatchChain)
	}
	// Right ticker, wrong address — AddressKnown but NOT Verified
	res = r.MatchByAddress("gate", "USDT", "ethereum", "0x0000000000000000000000000000000000000000")
	if res.Verified {
		t.Errorf("expected Verified=false on address mismatch: %+v", res)
	}
	if !res.AddressKnown {
		t.Errorf("expected AddressKnown=true since ticker exists")
	}
	// Unknown venue
	res = r.MatchByAddress("binance", "USDT", "ethereum", "0xdac17f958d2ee523a2206206994597c13d831ec7")
	if res.Verified || res.AddressKnown {
		t.Errorf("expected zero MatchResult for unknown venue: %+v", res)
	}
	// Unknown ticker on a known venue
	res = r.MatchByAddress("gate", "DOGE-X", "ethereum", "0xdac17f958d2ee523a2206206994597c13d831ec7")
	if res.Verified || res.AddressKnown {
		t.Errorf("expected zero MatchResult for unknown ticker: %+v", res)
	}
}

// TestPersistRoundTrip — PersistToDisk + LoadFromDisk yields the same map.
func TestPersistRoundTrip(t *testing.T) {
	tmp := t.TempDir()
	r := NewRegistry(tmp)
	r.SetVenue("kucoin", VenueAssets{
		"BTC": {{Chain: "ethereum", Address: "0xwbtc-address"}},
	})
	if err := r.PersistToDisk(); err != nil {
		t.Fatalf("persist: %v", err)
	}
	r2 := NewRegistry(tmp)
	if err := r2.LoadFromDisk(); err != nil {
		t.Fatalf("load: %v", err)
	}
	got := r2.SizeByVenue()
	if got["kucoin"] != 1 {
		t.Errorf("expected 1 kucoin ticker after roundtrip, got %+v", got)
	}
}

// TestChainNormalize — every CEX alias points to a known DexScreener chain.
func TestChainNormalize(t *testing.T) {
	cases := map[string]string{
		"ETH":               "ethereum",
		"ERC20":             "ethereum",
		"eth-erc20":         "ethereum",
		"BSC":               "bsc",
		"BEP20(BSC)":        "bsc",
		"BNB Smart Chain":   "bsc",
		"MATIC":             "polygon",
		"Polygon":           "polygon",
		"ARBITRUM":          "arbitrum",
		"Arbitrum One":      "arbitrum",
		"OP":                "optimism",
		"Solana":            "solana",
		"SOL":               "solana",
		"unknown chain":     "",
		"":                  "",
	}
	for in, want := range cases {
		if got := NormalizeChain(in); got != want {
			t.Errorf("NormalizeChain(%q) = %q; want %q", in, got, want)
		}
	}
}
