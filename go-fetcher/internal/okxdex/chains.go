package okxdex

import "context"

type chainRow struct {
	ChainIndex string `json:"chainIndex"`
	ChainName  string `json:"chainName"`
}

// FetchChains returns the chainIndex → chainName map from the market
// supported-chain list.
func (c *Client) FetchChains(ctx context.Context) (map[string]string, error) {
	var rows []chainRow
	if err := c.do(ctx, "GET", "/api/v6/dex/market/supported/chain", nil, &rows); err != nil {
		return nil, err
	}
	out := make(map[string]string, len(rows))
	for _, r := range rows {
		if r.ChainIndex != "" {
			out[r.ChainIndex] = r.ChainName
		}
	}
	return out, nil
}
