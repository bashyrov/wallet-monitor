// KuCoin spot orderbook WS.
//
// URL: dynamically fetched via POST https://api.kucoin.com/api/v1/bullet-public
// (different host from futures bullet-public). Same token + connectId
// query-string handshake.
//
// Topic: /spotMarket/level2Depth50:<BASE>-USDT (note dash, vs futures
// XBTUSDTM concatenation).
//
// All other quirks shared with futures: SubscribeDelay 400ms, app-level
// {"id","type":"ping"} keepalive, no lib pings.
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

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const spotBulletEndpoint = "https://api.kucoin.com/api/v1/bullet-public"

type spotAuthClient struct {
	mu     sync.Mutex
	cached *tokenInfo
}

func (c *spotAuthClient) FetchURL(ctx context.Context) (string, time.Duration, error) {
	c.mu.Lock()
	if c.cached != nil && time.Now().Before(c.cached.expires.Add(-30*time.Second)) {
		endpoint, token, pingInt := c.cached.endpoint, c.cached.token, c.cached.pingInt
		c.mu.Unlock()
		return buildKuCoinURL(endpoint, token, "avls"), pingInt, nil
	}
	c.mu.Unlock()

	req, err := http.NewRequestWithContext(ctx, "POST", spotBulletEndpoint, nil)
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
			} `json:"instanceServers"`
		} `json:"data"`
	}
	if err := sonic.Unmarshal(body, &doc); err != nil {
		return "", 0, err
	}
	if doc.Code != "200000" || doc.Data.Token == "" || len(doc.Data.Servers) == 0 {
		return "", 0, errors.New("kucoin-spot bullet-public: bad response")
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
	return buildKuCoinURL(srv.Endpoint, doc.Data.Token, "avls"), pingInt, nil
}

type Spot struct {
	store *cache.Store
	auth  *spotAuthClient
}

func NewSpot(store *cache.Store) *ws.Runner {
	a := &Spot{store: store, auth: &spotAuthClient{}}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("kucoin_spot", snap.Symbol, snap, "ws")
	})
}

func (a *Spot) Name() string { return "kucoin_spot" }

func (a *Spot) URL(ctx context.Context) (string, error) {
	u, _, err := a.auth.FetchURL(ctx)
	return u, err
}

func (a *Spot) BuildSubscribe(symbols []string) [][]byte {
	frames := make([][]byte, 0, (len(symbols)+kucoinBatch-1)/kucoinBatch)
	for i := 0; i < len(symbols); i += kucoinBatch {
		end := i + kucoinBatch
		if end > len(symbols) {
			end = len(symbols)
		}
		topics := make([]string, end-i)
		for j, s := range symbols[i:end] {
			topics[j] = strings.ToUpper(s) + "-USDT"
		}
		f := map[string]any{
			"id":             time.Now().UnixNano() + int64(i),
			"type":           "subscribe",
			"topic":          "/spotMarket/level2Depth50:" + strings.Join(topics, ","),
			"privateChannel": false,
			"response":       true,
		}
		b, _ := ws.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

func (a *Spot) Parse(frame []byte) (*ws.Snapshot, error) {
	var msg struct {
		Type  string `json:"type"`
		Topic string `json:"topic"`
		Data  struct {
			Bids [][]any `json:"bids"`
			Asks [][]any `json:"asks"`
		} `json:"data"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Type != "message" || !strings.HasPrefix(msg.Topic, "/spotMarket/level2Depth50:") {
		return nil, nil
	}
	pair := strings.TrimPrefix(msg.Topic, "/spotMarket/level2Depth50:")
	if !strings.HasSuffix(pair, "-USDT") {
		return nil, nil
	}
	token := strings.TrimSuffix(pair, "-USDT")

	parseSide := func(rows [][]any) []ws.Level {
		out := make([]ws.Level, 0, len(rows))
		for _, r := range rows {
			if len(r) < 2 {
				continue
			}
			var px, sz float64
			switch v := r[0].(type) {
			case string:
				_, _ = fmt.Sscanf(v, "%f", &px)
			case float64:
				px = v
			}
			switch v := r[1].(type) {
			case string:
				_, _ = fmt.Sscanf(v, "%f", &sz)
			case float64:
				sz = v
			}
			if sz > 0 {
				out = append(out, ws.Level{px, sz})
			}
		}
		return out
	}
	return &ws.Snapshot{
		Symbol: token,
		Bids:   parseSide(msg.Data.Bids),
		Asks:   parseSide(msg.Data.Asks),
	}, nil
}

func (a *Spot) Heartbeat() []byte {
	frame, _ := ws.MarshalJSON(map[string]any{"id": time.Now().UnixNano(), "type": "ping"})
	return frame
}
func (a *Spot) HeartbeatInterval() time.Duration { return 15 * time.Second }
func (a *Spot) PongFor(_ []byte) []byte          { return nil }
func (a *Spot) UseLibPings() bool                { return false }
func (a *Spot) SubscribeDelay() time.Duration    { return 400 * time.Millisecond }
func (a *Spot) MaxSymbols() int                  { return 100 }
func (a *Spot) DecompressGzip() bool             { return false }
func (a *Spot) OnReconnect()                     {}
