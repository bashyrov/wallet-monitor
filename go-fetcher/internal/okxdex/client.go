// Package okxdex is the OKX Web3 DEX API client — the sole on-chain
// price source for the dex-short screener mode (replaced CoinGecko +
// DexScreener 2026-07). All endpoints require project-scoped API creds;
// without them every call returns ErrNotConfigured and the dex compute
// degrades to an empty dex_arbitrage.json.
package okxdex

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"time"

	"github.com/bytedance/sonic"
)

var ErrNotConfigured = errors.New("okxdex: OKX_WEB3_* credentials not configured")

const baseURL = "https://www.okx.com"

type Client struct {
	apiKey     string
	secret     string
	passphrase string
	project    string
	http       *http.Client
}

func NewClientFromEnv() *Client {
	return &Client{
		apiKey:     os.Getenv("OKX_WEB3_API_KEY"),
		secret:     os.Getenv("OKX_WEB3_SECRET"),
		passphrase: os.Getenv("OKX_WEB3_PASSPHRASE"),
		project:    os.Getenv("OKX_WEB3_PROJECT_ID"),
		http:       &http.Client{Timeout: 15 * time.Second},
	}
}

func (c *Client) Configured() bool {
	return c.apiKey != "" && c.secret != "" && c.passphrase != "" && c.project != ""
}

// signRequest builds the OK-ACCESS-SIGN value:
// base64(HMAC-SHA256(secret, timestamp + method + requestPath + body)).
func signRequest(secret, timestamp, method, requestPath, body string) string {
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(timestamp + method + requestPath + body))
	return base64.StdEncoding.EncodeToString(mac.Sum(nil))
}

type envelope struct {
	Code string          `json:"code"`
	Msg  string          `json:"msg"`
	Data json.RawMessage `json:"data"`
}

// do issues a signed request. requestPath must include the query string
// (it is part of the sign preimage). out receives the decoded "data" field.
func (c *Client) do(ctx context.Context, method, requestPath string, body []byte, out any) error {
	if !c.Configured() {
		return ErrNotConfigured
	}
	ts := time.Now().UTC().Format("2006-01-02T15:04:05.000Z")
	var rdr io.Reader
	if len(body) > 0 {
		rdr = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, baseURL+requestPath, rdr)
	if err != nil {
		return err
	}
	req.Header.Set("OK-ACCESS-KEY", c.apiKey)
	req.Header.Set("OK-ACCESS-SIGN", signRequest(c.secret, ts, method, requestPath, string(body)))
	req.Header.Set("OK-ACCESS-TIMESTAMP", ts)
	req.Header.Set("OK-ACCESS-PASSPHRASE", c.passphrase)
	req.Header.Set("OK-ACCESS-PROJECT", c.project)
	if len(body) > 0 {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 64<<20))
	if err != nil {
		return err
	}
	if resp.StatusCode != 200 {
		return fmt.Errorf("okxdex: %s %s → HTTP %d: %.200s", method, requestPath, resp.StatusCode, raw)
	}
	var env envelope
	if err := sonic.Unmarshal(raw, &env); err != nil {
		return fmt.Errorf("okxdex: %s decode: %w", requestPath, err)
	}
	if env.Code != "0" {
		return fmt.Errorf("okxdex: %s → code=%s msg=%q", requestPath, env.Code, env.Msg)
	}
	if out != nil {
		if err := sonic.Unmarshal(env.Data, out); err != nil {
			return fmt.Errorf("okxdex: %s data decode: %w", requestPath, err)
		}
	}
	return nil
}
