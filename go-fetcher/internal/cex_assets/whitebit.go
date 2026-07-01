package cex_assets

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"
)

// WhiteBIT `/api/v4/public/assets` — public, unsigned. Returns a dict
// keyed by asset symbol with per-currency deposit/withdraw flags AND
// a nested `networks.deposits[]` + `networks.withdraws[]` giving the
// per-chain names.
//
// Caveat: no contract addresses. WhiteBIT only publishes chain NAMES,
// not the token's contract per chain. Downstream MatchByAddress will
// therefore always fall through to ticker+chain fallback for WhiteBIT
// entries — same policy as htx. We still surface the flags so the UI
// can render deposit/withdraw pills.
//
// Public rate-limit ~5-10 req/s (no docs — one sweep every refresh
// interval is trivially safe).
func FetchWhiteBIT(ctx context.Context, client *http.Client) (VenueAssets, error) {
	if client == nil {
		client = &http.Client{Timeout: 15 * time.Second}
	}
	const url = "https://whitebit.com/api/v4/public/assets"
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
		return nil, fmt.Errorf("whitebit assets http %d", resp.StatusCode)
	}
	// Map of symbol → asset struct. Not an array.
	var raw map[string]struct {
		CanWithdraw bool `json:"can_withdraw"`
		CanDeposit  bool `json:"can_deposit"`
		Networks    struct {
			Deposits  []string `json:"deposits"`
			Withdraws []string `json:"withdraws"`
		} `json:"networks"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&raw); err != nil {
		return nil, err
	}
	out := make(VenueAssets, len(raw))
	for symbol, a := range raw {
		sym := strings.ToUpper(strings.TrimSpace(symbol))
		if sym == "" {
			continue
		}
		// Union of deposit + withdraw chain lists — one entry per unique
		// chain name. WhiteBIT's global can_deposit / can_withdraw are
		// AND'd with per-chain listing membership. If a chain appears in
		// networks.deposits[] it CAN receive; similarly withdraws[].
		seen := make(map[string]struct{}, 4)
		add := func(chainName string, canDep, canWd bool) {
			key := strings.TrimSpace(chainName)
			if key == "" {
				return
			}
			if _, dup := seen[key]; dup {
				return
			}
			seen[key] = struct{}{}
			canonical := NormalizeChain(key)
			if canonical == "" {
				// Unknown chain — skip. Registry lookup by chain would
				// never match anyway.
				return
			}
			// Compute per-network flags:
			//   deposit  = in deposits list AND global can_deposit
			//   withdraw = in withdraws list AND global can_withdraw
			dep := canDep
			wd := canWd
			out[sym] = append(out[sym], AssetAddress{
				Chain:    canonical,
				Address:  "", // WhiteBIT publishes no contract addr
				Deposit:  &dep,
				Withdraw: &wd,
			})
		}
		for _, c := range a.Networks.Deposits {
			// A chain in deposits[] MAY still be missing from withdraws[]
			// — check both lists for the per-chain withdraw flag.
			inWithdraw := false
			for _, w := range a.Networks.Withdraws {
				if strings.EqualFold(w, c) {
					inWithdraw = true
					break
				}
			}
			add(c, a.CanDeposit, a.CanWithdraw && inWithdraw)
		}
		for _, w := range a.Networks.Withdraws {
			inDeposit := false
			for _, d := range a.Networks.Deposits {
				if strings.EqualFold(d, w) {
					inDeposit = true
					break
				}
			}
			add(w, a.CanDeposit && inDeposit, a.CanWithdraw)
		}
	}
	return out, nil
}
