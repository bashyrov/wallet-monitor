package binancealpha

import (
	"testing"

	"github.com/bytedance/sonic"
)

// Captured from the live endpoint 2026-07-05, trimmed to the fields the
// client models. GUA is deliberately offline+offsell on Binance's side
// while still carrying a live on-chain price — the index must keep it.
const fixture = `{
	"code": "000000",
	"message": null,
	"data": [
		{
			"tokenId": "CDC8B96BFEC126164F2B63D525FA15C9",
			"chainId": "56",
			"chainName": "BSC",
			"contractAddress": "0xA5C8E1513B6a08334B479FE4d71F1253259469bE",
			"name": "SUPERFORTUNE",
			"symbol": "GUA",
			"price": "0.05907497101799587527133429183834493",
			"percentChange24h": "0.18",
			"volume24h": "939173.34426018103678694573",
			"liquidity": "1431791.83973769888317489146287864464922",
			"offline": true,
			"offsell": true,
			"fullyDelisted": false
		},
		{
			"tokenId": "6A4E6C8E4A4B4E0F9A1B2C3D4E5F6071",
			"chainId": "-1",
			"chainName": "Solana",
			"contractAddress": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
			"name": "Bonk",
			"symbol": "BONK",
			"price": "0.0000121",
			"volume24h": "12345678.9",
			"liquidity": "9876543.21",
			"offline": false,
			"offsell": false,
			"fullyDelisted": false
		},
		{
			"tokenId": "DEAD",
			"chainId": "56",
			"chainName": "BSC",
			"contractAddress": "0xdead",
			"name": "Gone",
			"symbol": "GONE",
			"price": "0.5",
			"volume24h": "1",
			"liquidity": "1",
			"fullyDelisted": true
		}
	],
	"success": true
}`

func TestParseAndIndex(t *testing.T) {
	var env envelope
	if err := sonic.Unmarshal([]byte(fixture), &env); err != nil {
		t.Fatalf("envelope decode: %v", err)
	}
	if !env.Success || env.Code != "000000" {
		t.Fatalf("envelope = code=%q success=%v", env.Code, env.Success)
	}
	if len(env.Data) != 3 {
		t.Fatalf("rows = %d, want 3", len(env.Data))
	}

	s := NewService(nil)
	s.tokens = buildIndex(env.Data)

	gua := s.LookupBySymbol("gua")
	if len(gua) != 1 {
		t.Fatalf("GUA refs = %d, want 1 (offline tokens must be kept)", len(gua))
	}
	if gua[0].Chain != "bsc" {
		t.Errorf("GUA chain = %q, want bsc", gua[0].Chain)
	}
	if gua[0].Address != "0xA5C8E1513B6a08334B479FE4d71F1253259469bE" {
		t.Errorf("GUA address = %q", gua[0].Address)
	}
	if gua[0].Price < 0.059 || gua[0].Price > 0.06 {
		t.Errorf("GUA price = %v", gua[0].Price)
	}
	if gua[0].LiquidityUSD < 1.4e6 || gua[0].LiquidityUSD > 1.5e6 {
		t.Errorf("GUA liquidity = %v", gua[0].LiquidityUSD)
	}

	bonk := s.LookupBySymbol("BONK")
	if len(bonk) != 1 || bonk[0].Chain != "solana" {
		t.Fatalf("BONK = %+v", bonk)
	}
	if bonk[0].Address != "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263" {
		t.Errorf("BONK address = %q (base58 case must be preserved)", bonk[0].Address)
	}

	if refs := s.LookupBySymbol("GONE"); len(refs) != 0 {
		t.Errorf("fullyDelisted token indexed: %+v", refs)
	}
	if n := s.TokenSymbolCount(); n != 2 {
		t.Errorf("TokenSymbolCount = %d, want 2", n)
	}
}

func TestCanonicalChain(t *testing.T) {
	cases := map[string]string{
		"BSC": "bsc", "Ethereum": "ethereum", "Base": "base",
		"Solana": "solana", "TRON": "tron", "Arbitrum": "arbitrum",
		"Sui": "sui", "Sonic": "sonic", "Linea": "linea",
		"Some New Chain": "somenewchain",
	}
	for in, want := range cases {
		if got := canonicalChain(in); got != want {
			t.Errorf("canonicalChain(%q) = %q, want %q", in, got, want)
		}
	}
}
