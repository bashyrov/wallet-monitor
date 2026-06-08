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

// Binance `GET /sapi/v1/capital/config/getall` is the canonical address
// source. Security type USER_DATA — read-only API key is sufficient
// (verified against official docs + Binance Spot API SecurityType spec
// + python-binance SDK). NO withdrawal permission required.
//
// Signing per Binance Spot spec:
//   timestamp = current ms
//   recvWindow = 5000 (default)
//   query = sorted(params) + "&timestamp=..&recvWindow=.."
//   signature = HMAC-SHA256(secret, query) as hex
//   final URL = base + path + "?" + query + "&signature=" + signature
//   header X-MBX-APIKEY = key
//
// Schema:
//   rows[].coin
//   rows[].networkList[].network        (Binance chain id, e.g. "ETH", "BSC")
//   rows[].networkList[].contractAddress (empty for L1 natives)
//
// Doc: https://developers.binance.com/docs/wallet/capital
func FetchBinance(ctx context.Context, client *http.Client, creds SignedCreds) (VenueAssets, error) {
	if !creds.HasKey() {
		return nil, fmt.Errorf("binance: no creds in env (CEX_BINANCE_READ_KEY + CEX_BINANCE_READ_SECRET)")
	}
	if client == nil {
		client = &http.Client{Timeout: 20 * time.Second}
	}
	const path = "/sapi/v1/capital/config/getall"
	const base = "https://api.binance.com"

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
	req.Header.Set("X-MBX-APIKEY", creds.APIKey)
	req.Header.Set("Accept", "application/json")

	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		// Body may contain {"code":-2014, "msg":"..."} on bad key — log
		// the venue + status only, never include creds.
		return nil, fmt.Errorf("binance capital-config http %d", resp.StatusCode)
	}
	var rows []struct {
		Coin        string `json:"coin"`
		NetworkList []struct {
			Network         string `json:"network"`
			ContractAddress string `json:"contractAddress"`
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
