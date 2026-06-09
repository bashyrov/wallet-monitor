package cex_assets

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"
)

// Gate `/api/v4/spot/currencies` is the cleanest public endpoint for
// per-chain contract addresses. Returns one row per currency with a
// nested `chains[]` array (NOT flat per-chain rows as their docs
// sometimes suggest). Schema fields we consume:
//
//   currency:           "USDT"
//   delisted:           bool         — skip true (asset removed entirely)
//   trade_disabled:     bool         — skip true (no trading possible)
//   chains[].name:      "ETH"        — Gate's chain id, normalised below
//   chains[].addr:      "0xdAC1..."  — contract address (lowercase'd here)
//   chains[].deposit_disabled / withdraw_disabled — skip if both true
//
// Public, unsigned. Rate-limit 200 req per 10s per IP — one sweep
// fits trivially.
func FetchGate(ctx context.Context, client *http.Client) (VenueAssets, error) {
	if client == nil {
		client = &http.Client{Timeout: 15 * time.Second}
	}
	const url = "https://api.gateio.ws/api/v4/spot/currencies"
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
		return nil, fmt.Errorf("gate currencies http %d", resp.StatusCode)
	}
	var rows []struct {
		Currency      string `json:"currency"`
		Delisted      bool   `json:"delisted"`
		TradeDisabled bool   `json:"trade_disabled"`
		Chains        []struct {
			Name              string `json:"name"`
			Addr              string `json:"addr"`
			DepositDisabled   bool   `json:"deposit_disabled"`
			WithdrawDisabled  bool   `json:"withdraw_disabled"`
		} `json:"chains"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&rows); err != nil {
		return nil, err
	}
	out := make(VenueAssets, 1024)
	for _, r := range rows {
		if r.Delisted || r.TradeDisabled {
			continue
		}
		ticker := strings.ToUpper(strings.TrimSpace(r.Currency))
		if ticker == "" {
			continue
		}
		for _, ch := range r.Chains {
			// Don't pre-filter on transfer status: even when deposit or
			// withdraw is off, the entry's value to the user is "address
			// verified BUT transfer disabled" — that's the hazard we
			// surface, not hide. Only skip on schema problems.
			canon := NormalizeChain(ch.Name)
			if canon == "" {
				// Unknown CEX chain id (e.g. TRX, KCC, BTC native) —
				// skip rather than emit unmappable data. Caller's
				// MatchByAddress treats absent rows as "address unknown".
				continue
			}
			addr := strings.ToLower(strings.TrimSpace(ch.Addr))
			if addr == "" {
				continue
			}
			// Gate exposes inverted flags (disabled, not enabled). Flip
			// to positive sense so the registry contract is consistent
			// across venues. *bool so consumers can distinguish "we
			// know it's off" (&false) from "we don't know" (nil) —
			// matters because the UI must NOT show unknown as enabled.
			dep := !ch.DepositDisabled
			wd := !ch.WithdrawDisabled
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
