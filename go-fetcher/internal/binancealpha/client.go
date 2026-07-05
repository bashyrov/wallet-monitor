// Package binancealpha is the Binance Alpha public token-list client —
// a complementary on-chain price source for the dex-short screener mode
// alongside okxdex. One unauthenticated endpoint returns every Alpha
// listing (~650 tokens across 9 chains) with price, liquidity and 24h
// volume in the same payload, so no separate price sweep is needed.
package binancealpha

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/bytedance/sonic"
)

const tokenListURL = "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"

type Client struct {
	http *http.Client
}

func NewClient() *Client {
	return &Client{http: &http.Client{Timeout: 30 * time.Second}}
}

// Token is the wire row. Numeric fields arrive as strings; offline /
// offsell flags are intentionally NOT modelled — tokens Binance has
// pulled from its own Alpha trading UI (e.g. GUA) still carry live
// on-chain prices, and dropping them would lose exactly the long-tail
// coverage this source exists for. Only fullyDelisted rows are dead.
type Token struct {
	ChainID         string `json:"chainId"`
	ChainName       string `json:"chainName"`
	ContractAddress string `json:"contractAddress"`
	Name            string `json:"name"`
	Symbol          string `json:"symbol"`
	Price           string `json:"price"`
	Volume24h       string `json:"volume24h"`
	Liquidity       string `json:"liquidity"`
	FullyDelisted   bool   `json:"fullyDelisted"`
}

type envelope struct {
	Code    string  `json:"code"`
	Message *string `json:"message"`
	Data    []Token `json:"data"`
	Success bool    `json:"success"`
}

func (c *Client) FetchTokens(ctx context.Context) ([]Token, error) {
	req, err := http.NewRequestWithContext(ctx, "GET", tokenListURL, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36")
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 32<<20))
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("binancealpha: token/list → HTTP %d: %.200s", resp.StatusCode, raw)
	}
	var env envelope
	if err := sonic.Unmarshal(raw, &env); err != nil {
		return nil, fmt.Errorf("binancealpha: token/list decode: %w", err)
	}
	if !env.Success || env.Code != "000000" {
		msg := ""
		if env.Message != nil {
			msg = *env.Message
		}
		return nil, fmt.Errorf("binancealpha: token/list → code=%s msg=%q", env.Code, msg)
	}
	return env.Data, nil
}
