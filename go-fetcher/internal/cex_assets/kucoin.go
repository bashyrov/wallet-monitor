package cex_assets

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"
)

// KuCoin `/api/v3/currencies` is the canonical source for per-chain
// contract addresses. v3 schema (v1/v2 lacked addresses) returns the
// full coin list with a `chains[]` array; each entry has:
//
//   chainName:       "ETH"
//   chainId:         "eth"
//   contractAddress: "0xdac17f958..."
//
// Public, unsigned. Weight 3 — fine for once-daily refresh.
//
// Doc:
//   https://www.kucoin.com/docs/rest/spot-trading/market-data/get-currency-list
func FetchKuCoin(ctx context.Context, client *http.Client) (VenueAssets, error) {
	if client == nil {
		client = &http.Client{Timeout: 15 * time.Second}
	}
	const url = "https://api.kucoin.com/api/v3/currencies"
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
		return nil, fmt.Errorf("kucoin currencies http %d", resp.StatusCode)
	}
	var doc struct {
		Code string `json:"code"`
		Data []struct {
			Currency string `json:"currency"`
			Chains   []struct {
				ChainName       string `json:"chainName"`
				ChainID         string `json:"chainId"`
				ContractAddress string `json:"contractAddress"`
				IsDepositEnabled bool  `json:"isDepositEnabled"`
				IsWithdrawEnabled bool `json:"isWithdrawEnabled"`
			} `json:"chains"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&doc); err != nil {
		return nil, err
	}
	if doc.Code != "200000" {
		return nil, fmt.Errorf("kucoin currencies code=%s", doc.Code)
	}
	out := make(VenueAssets, 1024)
	for _, c := range doc.Data {
		ticker := strings.ToUpper(strings.TrimSpace(c.Currency))
		if ticker == "" {
			continue
		}
		for _, ch := range c.Chains {
			// Don't pre-filter on transfer status: even when deposit or
			// withdraw is off, the entry's value to the user is "address
			// verified BUT transfer disabled" — that's the hazard we
			// surface, not hide.
			raw := ch.ChainID
			if raw == "" {
				raw = ch.ChainName
			}
			canon := NormalizeChain(raw)
			if canon == "" {
				continue
			}
			addr := strings.ToLower(strings.TrimSpace(ch.ContractAddress))
			if addr == "" {
				continue
			}
			dep := ch.IsDepositEnabled
			wd := ch.IsWithdrawEnabled
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
