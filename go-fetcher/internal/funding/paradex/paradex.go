// Package paradex — funding adapter for Paradex StarkNet perp-DEX.
//
// REST: GET https://api.prod.paradex.trade/v1/markets/summary?market=ALL
// returns {"results":[{symbol, mark_price, funding_rate, volume_24h, ...}]}.
//
// Only -USD-PERP markets are kept; spot options (-P/-C suffix) are ignored.
// volume_24h is in base-asset units — multiplied by mark_price for USD.
// Funding interval is 8h (confirmed via /v1/funding/data timestamps).
// Next-funding boundary = next 00/08/16 UTC.
//
// REST-only: Paradex has a public WS (wss://ws.api.prod.paradex.trade/v1)
// but DEX user-base is small enough that 5-min REST polling is sufficient.
package paradex

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/bytedance/sonic"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding"
)

const restURL = "https://api.prod.paradex.trade/v1/markets/summary?market=ALL"

type Adapter struct{}

func New() *Adapter { return &Adapter{} }

func (a *Adapter) Name() string                             { return "paradex" }
func (a *Adapter) URL(_ context.Context) (string, error)   { return "", nil }
func (a *Adapter) BuildSubscribe(_ []string) [][]byte      { return nil }
func (a *Adapter) ParseWS(_ []byte) ([]funding.Tick, error) { return nil, nil }
func (a *Adapter) Heartbeat() []byte                       { return nil }
func (a *Adapter) HeartbeatInterval() time.Duration        { return 0 }
func (a *Adapter) PongFor(_ []byte) []byte                 { return nil }
func (a *Adapter) UseLibPings() bool                       { return false }
func (a *Adapter) DecompressGzip() bool                    { return false }

func (a *Adapter) BackstopFetch(ctx context.Context, _ []string) ([]funding.Tick, error) {
	body, err := getJSON(ctx, restURL)
	if err != nil {
		return nil, err
	}

	var doc struct {
		Results []struct {
			Symbol    string  `json:"symbol"`
			MarkPrice float64 `json:"mark_price"`
			// Paradex also sends strings — handle via interface below
			FundingRate interface{} `json:"funding_rate"`
			Volume24h   float64     `json:"volume_24h"`
		} `json:"results"`
	}
	if err := sonic.Unmarshal(body, &doc); err != nil {
		return nil, err
	}

	const suffix = "-USD-PERP"
	const intervalH = 8.0
	const intervalS = int64(8 * 3600)

	now := time.Now().Unix()
	nextFunding := time.Unix((now/intervalS+1)*intervalS, 0)

	out := make([]funding.Tick, 0, len(doc.Results))
	for _, r := range doc.Results {
		if !strings.HasSuffix(r.Symbol, suffix) {
			continue
		}
		base := strings.TrimSuffix(r.Symbol, suffix)
		if base == "" || r.MarkPrice <= 0 {
			continue
		}
		rate := toFloat64(r.FundingRate)
		out = append(out, funding.Tick{
			Symbol:      base,
			Rate:        rate,
			MarkPrice:   r.MarkPrice,
			Volume24h:   r.Volume24h * r.MarkPrice,
			NextFunding: nextFunding,
			IntervalH:   intervalH,
		})
	}
	if len(out) == 0 {
		return nil, errors.New("paradex: empty results")
	}
	return out, nil
}

func (a *Adapter) BackstopInterval() time.Duration { return 5 * time.Minute }

func toFloat64(v interface{}) float64 {
	switch x := v.(type) {
	case float64:
		return x
	case string:
		var f float64
		_ = sonic.Unmarshal([]byte(x), &f)
		return f
	}
	return 0
}

func getJSON(ctx context.Context, url string) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", "Mozilla/5.0 avalant-fetcher/go")
	cl := &http.Client{Timeout: 10 * time.Second}
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
