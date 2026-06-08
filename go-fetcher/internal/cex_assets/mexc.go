package cex_assets

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// MEXC `GET /api/v3/capital/config/getall` is a Binance-clone schema
// with the contract field renamed: `contract` not `contractAddress`.
// Signing identical to Binance (HMAC-SHA256 hex, X-MEXC-APIKEY header).
//
// Schema:
//   rows[].coin
//   rows[].networkList[].network         (MEXC chain id; e.g. "BEP20(BSC)", "ERC20")
//   rows[].networkList[].contract        (the field — NOT contractAddress)
//
// Doc: https://mexcdevelop.github.io/apidocs/spot_v3_en/#query-the-currency-information
func FetchMEXC(ctx context.Context, client *http.Client, creds SignedCreds) (VenueAssets, error) {
	if !creds.HasKey() {
		return nil, fmt.Errorf("mexc: no creds in env (CEX_MEXC_READ_KEY + CEX_MEXC_READ_SECRET)")
	}
	if client == nil {
		client = &http.Client{Timeout: 20 * time.Second}
	}
	const path = "/api/v3/capital/config/getall"
	const base = "https://api.mexc.com"

	ts := strconv.FormatInt(time.Now().UnixMilli(), 10)
	params := SortedQuery(map[string]string{
		"timestamp":  ts,
		"recvWindow": "5000",
	})
	sig := HMACHex(creds.APISecret, params)
	full := base + path + "?" + params + "&signature=" + sig

	req, err := http.NewRequestWithContext(ctx, "GET", full, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("X-MEXC-APIKEY", creds.APIKey)
	req.Header.Set("Accept", "application/json")

	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("mexc capital-config http %d", resp.StatusCode)
	}
	var rows []struct {
		Coin        string `json:"coin"`
		NetworkList []struct {
			Network  string `json:"network"`
			Contract string `json:"contract"` // <-- not contractAddress
		} `json:"networkList"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&rows); err != nil {
		return nil, err
	}
	out := make(VenueAssets, 1024)
	for _, r := range rows {
		ticker := strings.ToUpper(strings.TrimSpace(r.Coin))
		if ticker == "" {
			continue
		}
		for _, n := range r.NetworkList {
			addr := strings.ToLower(strings.TrimSpace(n.Contract))
			if addr == "" {
				continue
			}
			// MEXC chain strings are noisy: "BEP20(BSC)" or "ERC20" or
			// "TRC20" — chain_norm has aliases for the common ones.
			canon := NormalizeChain(n.Network)
			if canon == "" {
				continue
			}
			out[ticker] = append(out[ticker], AssetAddress{Chain: canon, Address: addr})
		}
	}
	return out, nil
}
