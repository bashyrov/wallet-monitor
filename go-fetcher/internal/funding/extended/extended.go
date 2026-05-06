// Package extended — funding adapter for Extended StarkNet perp-DEX.
//
// REST: GET https://api.starknet.extended.exchange/api/v1/info/markets
// returns {"data":[{name, status, active, marketStats:{lastPrice, markPrice,
// fundingRate, dailyVolume, nextFundingRate}}]}.
//
// Only ACTIVE markets with name ending in "-USD" are kept.
// Token = name[:-4] (strip "-USD").
// nextFundingRate is a unix-ms timestamp from the exchange.
// Funding interval is 1h (confirmed by inspecting timestamps).
package extended

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

const restURL = "https://api.starknet.extended.exchange/api/v1/info/markets"

type Adapter struct{}

func New() *Adapter { return &Adapter{} }

func (a *Adapter) Name() string                             { return "extended" }
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
		Data []struct {
			Name   string `json:"name"`
			Status string `json:"status"`
			Active bool   `json:"active"`
			Stats  struct {
				LastPrice       interface{} `json:"lastPrice"`       // API returns string
				MarkPrice       interface{} `json:"markPrice"`       // API returns string
				FundingRate     interface{} `json:"fundingRate"`     // API returns string
				DailyVolume     interface{} `json:"dailyVolume"`     // API returns string
				NextFundingRate int64       `json:"nextFundingRate"` // unix ms, numeric
			} `json:"marketStats"`
		} `json:"data"`
	}
	if err := sonic.Unmarshal(body, &doc); err != nil {
		return nil, err
	}

	out := make([]funding.Tick, 0, len(doc.Data))
	for _, m := range doc.Data {
		if m.Status != "ACTIVE" || !m.Active {
			continue
		}
		if !strings.HasSuffix(m.Name, "-USD") {
			continue
		}
		token := m.Name[:len(m.Name)-4]
		if token == "" {
			continue
		}
		price := funding.ParseFloat(m.Stats.MarkPrice)
		if price == 0 {
			price = funding.ParseFloat(m.Stats.LastPrice)
		}
		rate := funding.ParseFloat(m.Stats.FundingRate)
		if price <= 0 || rate == 0 {
			continue
		}
		var nextFunding time.Time
		if m.Stats.NextFundingRate > 0 {
			nextFunding = time.Unix(m.Stats.NextFundingRate/1000, 0)
		}
		out = append(out, funding.Tick{
			Symbol:      token,
			Rate:        rate,
			MarkPrice:   price,
			Volume24h:   funding.ParseFloat(m.Stats.DailyVolume),
			NextFunding: nextFunding,
			IntervalH:   1.0,
		})
	}
	if len(out) == 0 {
		return nil, errors.New("extended: empty results")
	}
	return out, nil
}

func (a *Adapter) BackstopInterval() time.Duration { return 5 * time.Minute }

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
