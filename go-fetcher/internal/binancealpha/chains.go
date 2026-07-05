package binancealpha

import "strings"

// chainNameCanonical maps Binance Alpha's chainName display strings to
// the canonical lowercase chain ids the rest of the pipeline speaks
// (same ids as arb.okxChainCanonical output + cex_assets matching).
var chainNameCanonical = map[string]string{
	"BSC":      "bsc",
	"Ethereum": "ethereum",
	"Base":     "base",
	"Solana":   "solana",
	"TRON":     "tron",
	"Arbitrum": "arbitrum",
	"Sui":      "sui",
	"Sonic":    "sonic",
	"Linea":    "linea",
}

func canonicalChain(chainName string) string {
	if c, ok := chainNameCanonical[chainName]; ok {
		return c
	}
	return strings.ReplaceAll(strings.ToLower(chainName), " ", "")
}
