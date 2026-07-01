package cex_assets

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"
)

// Backpack `/api/v1/assets` — public, unsigned. Array of assets each
// with `tokens[]` per blockchain, giving contract address + per-network
// depositEnabled / withdrawEnabled flags.
//
// Public rate-limit ~100 req/s per Backpack docs (as of 2026-07). One
// sweep every refresh interval is trivially safe.
//
// Sample entry (SOL):
//
//	{
//	  "coingeckoId": "solana",
//	  "displayName": "Solana",
//	  "symbol": "SOL",
//	  "tokens": [
//	    {
//	      "blockchain": "Solana",
//	      "contractAddress": "So1",
//	      "depositEnabled": true,
//	      "withdrawEnabled": true
//	    }
//	  ]
//	}
//
// Note: Backpack uses SPL / Solana wrapping conventions — the native
// SOL token's contractAddress is "So1" (their placeholder), which we
// treat as an empty address so MatchByAddress falls through to ticker
// fallback for L1 natives.
func FetchBackpack(ctx context.Context, client *http.Client) (VenueAssets, error) {
	if client == nil {
		client = &http.Client{Timeout: 15 * time.Second}
	}
	const url = "https://api.backpack.exchange/api/v1/assets"
	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/json")
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("backpack assets http %d", resp.StatusCode)
	}
	var raw []struct {
		Symbol string `json:"symbol"`
		Tokens []struct {
			Blockchain      string `json:"blockchain"`
			ContractAddress string `json:"contractAddress"`
			DepositEnabled  bool   `json:"depositEnabled"`
			WithdrawEnabled bool   `json:"withdrawEnabled"`
		} `json:"tokens"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&raw); err != nil {
		return nil, err
	}
	out := make(VenueAssets, len(raw))
	for _, a := range raw {
		sym := strings.ToUpper(strings.TrimSpace(a.Symbol))
		if sym == "" {
			continue
		}
		seen := make(map[string]struct{}, 4)
		for _, t := range a.Tokens {
			canonical := NormalizeChain(t.Blockchain)
			if canonical == "" {
				// Unknown chain (Backpack sometimes exposes "Bitcoin",
				// "Cardano", etc. that we don't map for DexScreener).
				continue
			}
			if _, dup := seen[canonical]; dup {
				continue
			}
			seen[canonical] = struct{}{}
			// Backpack's placeholder "So1" for native SOL is a marker,
			// not a real address — strip it so the ticker fallback path
			// kicks in for L1 natives.
			addr := strings.ToLower(strings.TrimSpace(t.ContractAddress))
			if addr == "so1" || addr == "0x0" {
				addr = ""
			}
			dep := t.DepositEnabled
			wd := t.WithdrawEnabled
			out[sym] = append(out[sym], AssetAddress{
				Chain:    canonical,
				Address:  addr,
				Deposit:  &dep,
				Withdraw: &wd,
			})
		}
	}
	return out, nil
}
