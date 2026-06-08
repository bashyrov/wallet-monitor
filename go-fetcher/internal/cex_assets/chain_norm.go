// Package cex_assets resolves CEX-listed tokens to their on-chain
// contract addresses, normalised to DexScreener's canonical chain ids
// (ethereum, bsc, polygon, etc.). Used by dex/* arb computes to match
// DEX↔CEX pairs by ADDRESS — not ticker, which is unique only inside
// one venue. Different tokens can share a ticker (e.g. "US.HUOBI" on
// HTX ≠ "US" on Cetus), producing fake spreads at -67% etc.
//
// Coverage tiers (per user policy):
//   - Public adapters always run: gate, kucoin, bitget. These need no
//     API keys, run on every deploy by default.
//   - Signed adapters (binance, bybit, okx, mexc, bingx) run when their
//     env-configured API keys are present. Read-only perms required;
//     keys with withdraw perm are NEVER added to .env.
//   - htx exposes chain name but no address — always ticker+chain
//     fallback, marked address_unverified=true.
package cex_assets

import "strings"

// chainAliasToDexScreener maps every CEX-side spelling we've seen to the
// DexScreener canonical chain id. The DexScreener side (BaseToken in
// dex.go) uses these canonical ids verbatim, so matches are direct.
//
// Lookup returns "" for unknown chains — caller marks the row
// address_unverified=true rather than guessing. Better to surface
// "unverified" than to claim a match across a chain we haven't mapped.
//
// One source of truth. To extend coverage of a new chain, add aliases
// for every CEX variant here, NOT in individual adapters.
var chainAliasToDexScreener = map[string]string{
	// Ethereum mainnet
	"eth":              "ethereum",
	"ethereum":         "ethereum",
	"erc20":            "ethereum",
	"erc-20":           "ethereum",
	"eth-erc20":        "ethereum",
	"ethereum mainnet": "ethereum",

	// BNB Smart Chain
	"bsc":               "bsc",
	"bep20":             "bsc",
	"bep-20":            "bsc",
	"bep20(bsc)":        "bsc",
	"bnb smart chain":   "bsc",
	"bnb chain":         "bsc",
	"binance smart chain": "bsc",
	"bnb-bsc":           "bsc",

	// Polygon
	"matic":         "polygon",
	"polygon":       "polygon",
	"polygon pos":   "polygon",
	"matic-polygon": "polygon",

	// Arbitrum
	"arbitrum":          "arbitrum",
	"arbitrum one":      "arbitrum",
	"arb":               "arbitrum",
	"arbitrumone":       "arbitrum",
	"arb-arbitrum one":  "arbitrum",
	"arbi":              "arbitrum",

	// Optimism
	"optimism":      "optimism",
	"op":            "optimism",
	"opmain":        "optimism",
	"op mainnet":    "optimism",

	// Base
	"base":     "base",
	"baseevm":  "base",
	"base evm": "base",

	// Avalanche C-chain
	"avalanche":           "avalanche",
	"avax":                "avalanche",
	"avaxc":               "avalanche",
	"avalanche c-chain":   "avalanche",
	"avalanche c chain":   "avalanche",
	"c-chain":             "avalanche",

	// Solana
	"sol":    "solana",
	"solana": "solana",

	// Fantom
	"ftm":    "fantom",
	"fantom": "fantom",

	// zkSync Era
	"zksync":      "zksync",
	"zksync era":  "zksync",
	"zksyncera":   "zksync",

	// Linea
	"linea": "linea",

	// Scroll
	"scroll": "scroll",

	// Mantle
	"mantle": "mantle",

	// Blast
	"blast": "blast",

	// Sui — DexScreener supports
	"sui": "sui",

	// Aptos
	"apt":    "aptos",
	"aptos":  "aptos",

	// Sei
	"sei": "sei",
}

// NormalizeChain returns the DexScreener canonical chain id for a CEX
// chain string, or "" for unknown chains. Case-insensitive, trims
// whitespace. Native-chain L1 tokens (BTC, ETH itself, etc.) are
// out-of-scope — DexScreener tokens are smart-contract tokens by
// definition; this map only covers EVM + Solana/Aptos/Sui.
func NormalizeChain(cexChain string) string {
	if cexChain == "" {
		return ""
	}
	key := strings.ToLower(strings.TrimSpace(cexChain))
	return chainAliasToDexScreener[key]
}
