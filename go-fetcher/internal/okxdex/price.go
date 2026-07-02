package okxdex

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bytedance/sonic"
)

type PriceReq struct {
	ChainIndex           string `json:"chainIndex"`
	TokenContractAddress string `json:"tokenContractAddress"`
}

type priceRow struct {
	ChainIndex           string `json:"chainIndex"`
	TokenContractAddress string `json:"tokenContractAddress"`
	Price                string `json:"price"`
}

const priceChunkSize = 100

// PriceKey is the map key format returned by FetchPrices.
func PriceKey(chainIndex, address string) string {
	return chainIndex + ":" + strings.ToLower(address)
}

// FetchPrices batch-fetches latest token prices. Payload is chunked at
// 100 tokens per request. Returns map keyed by "chainIndex:loweraddr".
// Partial success is real success — a failed chunk is skipped and the
// error of the LAST failed chunk is returned alongside whatever resolved,
// so the caller can distinguish "fully starved" (empty map + err) from
// "degraded" (non-empty map + err).
func (c *Client) FetchPrices(ctx context.Context, refs []PriceReq) (map[string]float64, error) {
	out := make(map[string]float64, len(refs))
	var lastErr error
	for i := 0; i < len(refs); i += priceChunkSize {
		if i > 0 {
			// Pace chunks — same per-second rate limit as the rest of
			// the OKX Web3 API.
			select {
			case <-ctx.Done():
				return out, ctx.Err()
			case <-time.After(1100 * time.Millisecond):
			}
		}
		end := i + priceChunkSize
		if end > len(refs) {
			end = len(refs)
		}
		body, err := sonic.Marshal(refs[i:end])
		if err != nil {
			return out, err
		}
		var rows []priceRow
		if err := c.do(ctx, "POST", "/api/v6/dex/market/price", body, &rows); err != nil {
			lastErr = err
			continue
		}
		for _, r := range rows {
			px, err := strconv.ParseFloat(r.Price, 64)
			if err != nil || px <= 0 {
				continue
			}
			out[PriceKey(r.ChainIndex, r.TokenContractAddress)] = px
		}
	}
	return out, lastErr
}
