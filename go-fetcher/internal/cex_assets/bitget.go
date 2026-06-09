package cex_assets

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"
)

// Bitget `/api/v2/spot/public/coins` — fully public, returns the full
// coin list with `chains[]` per coin. Schema fields:
//
//   coin:            "USDT"
//   chains[].chain:           "ERC20"   (Bitget chain id, normalised)
//   chains[].contractAddress: "0xdac..."
//   chains[].withdrawable:    "true" | "false"  (string)
//   chains[].rechargeable:    "true" | "false"  (string)
//
// Doc:
//   https://www.bitget.com/api-doc/spot/public/Get-Coin-List
func FetchBitget(ctx context.Context, client *http.Client) (VenueAssets, error) {
	if client == nil {
		client = &http.Client{Timeout: 15 * time.Second}
	}
	const url = "https://api.bitget.com/api/v2/spot/public/coins"
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
		return nil, fmt.Errorf("bitget coins http %d", resp.StatusCode)
	}
	var doc struct {
		Code string `json:"code"`
		Data []struct {
			Coin   string `json:"coin"`
			Chains []struct {
				Chain           string `json:"chain"`
				ContractAddress string `json:"contractAddress"`
				Withdrawable    string `json:"withdrawable"`
				Rechargeable    string `json:"rechargeable"`
			} `json:"chains"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&doc); err != nil {
		return nil, err
	}
	if doc.Code != "00000" {
		return nil, fmt.Errorf("bitget coins code=%s", doc.Code)
	}
	out := make(VenueAssets, 1024)
	for _, c := range doc.Data {
		ticker := strings.ToUpper(strings.TrimSpace(c.Coin))
		if ticker == "" {
			continue
		}
		for _, ch := range c.Chains {
			// Don't pre-filter on transfer status: even when deposit or
			// withdraw is off, the entry's value to the user is "address
			// verified BUT transfer disabled" — that's the hazard we
			// surface, not hide.
			canon := NormalizeChain(ch.Chain)
			if canon == "" {
				continue
			}
			addr := strings.ToLower(strings.TrimSpace(ch.ContractAddress))
			if addr == "" {
				continue
			}
			// Bitget uses positive flags but as string "true"/"false".
			// "rechargeable" = deposit, "withdrawable" = withdraw.
			dep := strings.EqualFold(strings.TrimSpace(ch.Rechargeable), "true")
			wd := strings.EqualFold(strings.TrimSpace(ch.Withdrawable), "true")
			out[ticker] = append(out[ticker], AssetAddress{
				Chain:    canon,
				Address:  addr,
				Deposit:  &dep,
				Withdraw: &wd,
			})
		}
	}
	return out, nil
}
