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

// Bybit `/v5/asset/coin/query-info` is the canonical address source.
// Signed (v5 spec: timestamp || api_key || recv_window || query in the
// HMAC preimage). Read-only API key is sufficient — no withdrawal perm.
//
// Schema fields:
//   rows[].coin
//   rows[].chains[].chain         (Bybit chain id, normalised below)
//   rows[].chains[].chainType     (display name)
//   rows[].chains[].contractAddress (empty for L1 natives)
//
// Doc: https://bybit-exchange.github.io/docs/v5/asset/coin-info
func FetchBybit(ctx context.Context, client *http.Client, creds SignedCreds) (VenueAssets, error) {
	if !creds.HasKey() {
		return nil, fmt.Errorf("bybit: no creds in env (CEX_BYBIT_READ_KEY + CEX_BYBIT_READ_SECRET)")
	}
	if client == nil {
		client = &http.Client{Timeout: 20 * time.Second}
	}
	const path = "/v5/asset/coin/query-info"
	const base = "https://api.bybit.com"
	const recvWindow = "5000"
	ts := strconv.FormatInt(time.Now().UnixMilli(), 10)
	// Empty query → empty payload in the signature preimage.
	sig := HMACHexConcat(creds.APISecret, ts, creds.APIKey, recvWindow, "")

	req, err := http.NewRequestWithContext(ctx, "GET", base+path, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("X-BAPI-API-KEY", creds.APIKey)
	req.Header.Set("X-BAPI-SIGN", sig)
	req.Header.Set("X-BAPI-SIGN-TYPE", "2") // HMAC_SHA256
	req.Header.Set("X-BAPI-TIMESTAMP", ts)
	req.Header.Set("X-BAPI-RECV-WINDOW", recvWindow)
	req.Header.Set("Accept", "application/json")

	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("bybit coin-info http %d", resp.StatusCode)
	}
	var doc struct {
		RetCode int    `json:"retCode"`
		RetMsg  string `json:"retMsg"`
		Result  struct {
			Rows []struct {
				Coin   string `json:"coin"`
				Chains []struct {
					Chain           string `json:"chain"`
					ChainType       string `json:"chainType"`
					ContractAddress string `json:"contractAddress"`
				} `json:"chains"`
			} `json:"rows"`
		} `json:"result"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&doc); err != nil {
		return nil, err
	}
	if doc.RetCode != 0 {
		// Never log api_key/secret — only the venue + retCode + retMsg.
		return nil, fmt.Errorf("bybit retCode=%d msg=%s", doc.RetCode, doc.RetMsg)
	}
	out := make(VenueAssets, 1024)
	for _, r := range doc.Result.Rows {
		ticker := strings.ToUpper(strings.TrimSpace(r.Coin))
		if ticker == "" {
			continue
		}
		for _, ch := range r.Chains {
			addr := strings.ToLower(strings.TrimSpace(ch.ContractAddress))
			if addr == "" {
				continue
			}
			// Try chain first, fall back to chainType (Bybit varies)
			canon := NormalizeChain(ch.Chain)
			if canon == "" {
				canon = NormalizeChain(ch.ChainType)
			}
			if canon == "" {
				continue
			}
			out[ticker] = append(out[ticker], AssetAddress{Chain: canon, Address: addr})
		}
	}
	return out, nil
}
