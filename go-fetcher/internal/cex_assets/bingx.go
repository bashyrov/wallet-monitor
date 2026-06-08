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

// BingX `GET /openApi/wallets/v1/capital/config/getall`. Same Binance-
// clone signing (HMAC hex over sorted query, X-BX-APIKEY header).
//
// Schema:
//   rows[].coin
//   rows[].networkList[].network          (BingX chain id)
//   rows[].networkList[].contractAddress  (the field, like Binance)
//
// Doc: https://bingx-api.github.io/docs/spot/wallets/#all-coins-39-information
func FetchBingX(ctx context.Context, client *http.Client, creds SignedCreds) (VenueAssets, error) {
	if !creds.HasKey() {
		return nil, fmt.Errorf("bingx: no creds in env (CEX_BINGX_READ_KEY + CEX_BINGX_READ_SECRET)")
	}
	if client == nil {
		client = &http.Client{Timeout: 20 * time.Second}
	}
	const path = "/openApi/wallets/v1/capital/config/getall"
	const base = "https://open-api.bingx.com"

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
	req.Header.Set("X-BX-APIKEY", creds.APIKey)
	req.Header.Set("Accept", "application/json")

	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("bingx capital-config http %d", resp.StatusCode)
	}
	var doc struct {
		Code int    `json:"code"`
		Msg  string `json:"msg"`
		Data []struct {
			Coin        string `json:"coin"`
			NetworkList []struct {
				Network         string `json:"network"`
				ContractAddress string `json:"contractAddress"`
			} `json:"networkList"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&doc); err != nil {
		return nil, err
	}
	if doc.Code != 0 {
		return nil, fmt.Errorf("bingx code=%d msg=%s", doc.Code, doc.Msg)
	}
	out := make(VenueAssets, 1024)
	for _, r := range doc.Data {
		ticker := strings.ToUpper(strings.TrimSpace(r.Coin))
		if ticker == "" {
			continue
		}
		for _, n := range r.NetworkList {
			addr := strings.ToLower(strings.TrimSpace(n.ContractAddress))
			if addr == "" {
				continue
			}
			canon := NormalizeChain(n.Network)
			if canon == "" {
				continue
			}
			out[ticker] = append(out[ticker], AssetAddress{Chain: canon, Address: addr})
		}
	}
	return out, nil
}
