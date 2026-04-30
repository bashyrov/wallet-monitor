// Package hyperliquid — funding adapter for Hyperliquid L1 perp.
//
// REST: POST https://api.hyperliquid.xyz/info  body {"type":"metaAndAssetCtxs"}
// returns [meta, ctxs] where ctxs[i] aligns with meta.universe[i].
// ctxs[i] has: funding (per-hour rate), markPx, oraclePx, dayNtlVlm,
// openInterest, premium.
//
// REST-only — WS webData2 carries the same data but parsing it is
// heavier than the simple POST, and our 2s cadence is plenty for a
// venue with hourly funding settlement.
package hyperliquid

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strconv"
	"time"

	"github.com/bytedance/sonic"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding"
)

const restURL = "https://api.hyperliquid.xyz/info"

type Adapter struct{}

func New() *Adapter { return &Adapter{} }

func (a *Adapter) Name() string                          { return "hyperliquid" }
func (a *Adapter) URL(_ context.Context) (string, error) { return "", nil }
func (a *Adapter) BuildSubscribe(_ []string) [][]byte    { return nil }
func (a *Adapter) ParseWS(_ []byte) ([]funding.Tick, error) {
	return nil, nil
}
func (a *Adapter) Heartbeat() []byte                { return nil }
func (a *Adapter) HeartbeatInterval() time.Duration { return 0 }
func (a *Adapter) PongFor(_ []byte) []byte          { return nil }
func (a *Adapter) UseLibPings() bool                { return false }
func (a *Adapter) DecompressGzip() bool             { return false }

func (a *Adapter) BackstopFetch(ctx context.Context, _ []string) ([]funding.Tick, error) {
	body, err := postJSON(ctx, restURL, map[string]any{"type": "metaAndAssetCtxs"})
	if err != nil {
		return nil, err
	}

	// Response: [meta, ctxs] — array-of-2 elements, parsed individually.
	var doc []json.RawMessage
	if err := sonic.Unmarshal(body, &doc); err != nil {
		return nil, err
	}
	if len(doc) < 2 {
		return nil, errors.New("hyperliquid: malformed response")
	}
	var meta struct {
		Universe []struct {
			Name string `json:"name"`
		} `json:"universe"`
	}
	if err := sonic.Unmarshal(doc[0], &meta); err != nil {
		return nil, err
	}
	var ctxs []struct {
		Funding       string `json:"funding"`
		MarkPx        string `json:"markPx"`
		OraclePx      string `json:"oraclePx"`
		DayNtlVlm     string `json:"dayNtlVlm"`
		OpenInterest  string `json:"openInterest"`
	}
	if err := sonic.Unmarshal(doc[1], &ctxs); err != nil {
		return nil, err
	}
	if len(ctxs) != len(meta.Universe) {
		return nil, errors.New("hyperliquid: meta/ctxs len mismatch")
	}

	out := make([]funding.Tick, 0, len(ctxs))
	for i, c := range ctxs {
		token := meta.Universe[i].Name
		if token == "" {
			continue
		}
		// Hyperliquid quotes funding PER HOUR, not 8h — flag intervalH=1.
		rate, _ := strconv.ParseFloat(c.Funding, 64)
		mark, _ := strconv.ParseFloat(c.MarkPx, 64)
		oracle, _ := strconv.ParseFloat(c.OraclePx, 64)
		vol, _ := strconv.ParseFloat(c.DayNtlVlm, 64)
		oi, _ := strconv.ParseFloat(c.OpenInterest, 64)
		out = append(out, funding.Tick{
			Symbol:     token,
			Rate:       rate,
			MarkPrice:  mark,
			IndexPrice: oracle,
			Volume24h:  vol,
			OpenIntUSD: oi * mark,
			IntervalH:  1,
		})
	}
	return out, nil
}

func (a *Adapter) BackstopInterval() time.Duration { return 2 * time.Second }

func postJSON(ctx context.Context, url string, body any) ([]byte, error) {
	enc, err := sonic.Marshal(body)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, "POST", url, bytes.NewReader(enc))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", "Mozilla/5.0 avalant-fetcher/go")
	cl := &http.Client{Timeout: 8 * time.Second}
	resp, err := cl.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, errors.New("http " + resp.Status)
	}
	return io.ReadAll(resp.Body)
}
