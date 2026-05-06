// Package kucoin — KuCoin futures requires a dynamic WS endpoint + token,
// fetched via POST https://api-futures.kucoin.com/api/v1/bullet-public.
//
// Bug #17 from PLAN — without this auth flow, the WS rejects on connect.
package kucoin

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/bytedance/sonic"
)

const bulletEndpoint = "https://api-futures.kucoin.com/api/v1/bullet-public"

type tokenInfo struct {
	endpoint string        // WS server endpoint, without token/connectId
	token    string
	pingInt  time.Duration
	expires  time.Time
}

// buildKuCoinURL assembles the final WS URL with a fresh connectId each call.
// connectId must be unique per connection — reusing it across reconnects causes
// the server to reject or reset the session mid-subscribe.
func buildKuCoinURL(endpoint, token, prefix string) string {
	connectID := fmt.Sprintf("%s-%d", prefix, time.Now().UnixNano())
	sep := "?"
	if strings.Contains(endpoint, "?") {
		sep = "&"
	}
	return endpoint + sep + "token=" + token + "&connectId=" + connectID
}

type authClient struct {
	mu     sync.Mutex
	cached *tokenInfo
}

func (c *authClient) FetchURL(ctx context.Context) (string, time.Duration, error) {
	c.mu.Lock()
	if c.cached != nil && time.Now().Before(c.cached.expires.Add(-30*time.Second)) {
		endpoint, token, pingInt := c.cached.endpoint, c.cached.token, c.cached.pingInt
		c.mu.Unlock()
		return buildKuCoinURL(endpoint, token, "avlf"), pingInt, nil
	}
	c.mu.Unlock()

	req, err := http.NewRequestWithContext(ctx, "POST", bulletEndpoint, nil)
	if err != nil {
		return "", 0, err
	}
	req.Header.Set("User-Agent", "Mozilla/5.0 avalant-fetcher/go")
	cl := &http.Client{Timeout: 8 * time.Second}
	resp, err := cl.Do(req)
	if err != nil {
		return "", 0, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", 0, err
	}

	var doc struct {
		Code string `json:"code"`
		Data struct {
			Token   string `json:"token"`
			Servers []struct {
				Endpoint     string `json:"endpoint"`
				PingInterval int    `json:"pingInterval"`
				PingTimeout  int    `json:"pingTimeout"`
			} `json:"instanceServers"`
		} `json:"data"`
	}
	if err := sonic.Unmarshal(body, &doc); err != nil {
		return "", 0, err
	}
	if doc.Code != "200000" || doc.Data.Token == "" || len(doc.Data.Servers) == 0 {
		return "", 0, errors.New("kucoin bullet-public: bad response")
	}
	srv := doc.Data.Servers[0]
	pingInt := time.Duration(srv.PingInterval) * time.Millisecond
	if pingInt <= 0 {
		pingInt = 18 * time.Second
	}
	c.mu.Lock()
	c.cached = &tokenInfo{
		endpoint: srv.Endpoint,
		token:    doc.Data.Token,
		pingInt:  pingInt,
		expires:  time.Now().Add(50 * time.Minute),
	}
	c.mu.Unlock()
	return buildKuCoinURL(srv.Endpoint, doc.Data.Token, "avlf"), pingInt, nil
}
