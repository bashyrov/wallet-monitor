package cex_assets

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"
)

// OKX `GET /api/v5/asset/currencies` returns the address list. Private,
// signed (HMAC-SHA256 in base64) + passphrase. Returns only currencies
// enabled on YOUR account — so a brand-new read-only key may need spot
// trading permission enabled at the venue side to see the full universe.
//
// OKX signing per v5 spec:
//   ts  = ISO-8601 UTC timestamp "2024-01-01T00:00:00.000Z"
//   pre = ts + method + path + body  (body empty for GET)
//   sig = base64(HMAC-SHA256(secret, pre))
//   headers:
//     OK-ACCESS-KEY
//     OK-ACCESS-SIGN        (sig)
//     OK-ACCESS-TIMESTAMP   (ts)
//     OK-ACCESS-PASSPHRASE  (creds.Passphrase)
//
// Schema is flat rows per chain — each row is one (currency, chain):
//   data[].ccy        ("USDT")
//   data[].chain      ("USDT-ERC20", "USDT-BSC" — chain id includes ccy)
//   data[].ctAddr     (contract address)
//   data[].mainNet    (bool — "main net" version vs test/v2)
//
// Doc: https://www.okx.com/docs-v5/en/#funding-account-rest-api-get-currencies
func FetchOKX(ctx context.Context, client *http.Client, creds SignedCreds) (VenueAssets, error) {
	if !creds.HasKey() || creds.Passphrase == "" {
		return nil, fmt.Errorf("okx: need CEX_OKX_READ_KEY + CEX_OKX_READ_SECRET + CEX_OKX_READ_PASSPHRASE")
	}
	if client == nil {
		client = &http.Client{Timeout: 20 * time.Second}
	}
	const path = "/api/v5/asset/currencies"
	const base = "https://www.okx.com"

	// OKX wants ISO ms-precision UTC: "2024-01-01T00:00:00.000Z".
	ts := time.Now().UTC().Format("2006-01-02T15:04:05.000Z")
	preimage := ts + "GET" + path
	sig := HMACBase64(creds.APISecret, preimage)

	req, err := http.NewRequestWithContext(ctx, "GET", base+path, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("OK-ACCESS-KEY", creds.APIKey)
	req.Header.Set("OK-ACCESS-SIGN", sig)
	req.Header.Set("OK-ACCESS-TIMESTAMP", ts)
	req.Header.Set("OK-ACCESS-PASSPHRASE", creds.Passphrase)
	req.Header.Set("Accept", "application/json")

	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("okx currencies http %d", resp.StatusCode)
	}
	var doc struct {
		Code string `json:"code"`
		Msg  string `json:"msg"`
		Data []struct {
			Ccy     string `json:"ccy"`
			Chain   string `json:"chain"`   // "USDT-ERC20"
			CtAddr  string `json:"ctAddr"`
			MainNet bool   `json:"mainNet"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&doc); err != nil {
		return nil, err
	}
	if doc.Code != "0" {
		return nil, fmt.Errorf("okx code=%s msg=%s", doc.Code, doc.Msg)
	}
	out := make(VenueAssets, 1024)
	for _, r := range doc.Data {
		ticker := strings.ToUpper(strings.TrimSpace(r.Ccy))
		if ticker == "" {
			continue
		}
		addr := strings.ToLower(strings.TrimSpace(r.CtAddr))
		if addr == "" {
			continue
		}
		// OKX chain string is "USDT-ERC20" / "USDT-BSC" / "USDT-Polygon"
		// etc. — strip the leading "<CCY>-" prefix so the normaliser
		// sees just the chain id. Done here vs in chain_norm because
		// it's OKX-specific schema quirk, not a chain alias.
		chainPart := r.Chain
		if i := strings.Index(chainPart, "-"); i >= 0 {
			chainPart = chainPart[i+1:]
		}
		canon := NormalizeChain(chainPart)
		if canon == "" {
			continue
		}
		out[ticker] = append(out[ticker], AssetAddress{Chain: canon, Address: addr})
	}
	return out, nil
}
