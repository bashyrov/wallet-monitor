// Package bingx — funding adapter for BingX swap.
//
// BingX caps WS at ~100 symbols per connection (same as orderbook bug
// #5 territory), and our Python adapter found the funding feed less
// reliable than REST. We use REST-only here; BingX's premiumIndex
// endpoint returns rate + nextFundingTime for every symbol in one call.
package bingx

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding"
)

const restURL = "https://open-api.bingx.com/openApi/swap/v2/quote/premiumIndex"

type Adapter struct{}

func New() *Adapter { return &Adapter{} }

func (a *Adapter) Name() string                          { return "bingx" }
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
	var doc struct {
		Data []struct {
			Symbol           string `json:"symbol"`
			MarkPrice        string `json:"markPrice"`
			IndexPrice       string `json:"indexPrice"`
			LastFundingRate  string `json:"lastFundingRate"`
			NextFundingTime  int64  `json:"nextFundingTime"`
		} `json:"data"`
	}
	if err := funding.HTTPGet(ctx, restURL, &doc); err != nil {
		return nil, err
	}
	out := make([]funding.Tick, 0, len(doc.Data))
	for _, r := range doc.Data {
		if !strings.HasSuffix(r.Symbol, "-USDT") {
			continue
		}
		token := strings.TrimSuffix(r.Symbol, "-USDT")
		mark, _ := strconv.ParseFloat(r.MarkPrice, 64)
		idx, _ := strconv.ParseFloat(r.IndexPrice, 64)
		rate, _ := strconv.ParseFloat(r.LastFundingRate, 64)
		t := funding.Tick{
			Symbol:    token,
			Rate:      rate,
			MarkPrice: mark,
			IndexPrice: idx,
			IntervalH: 8,
		}
		if r.NextFundingTime > 0 {
			t.NextFunding = time.UnixMilli(r.NextFundingTime)
		}
		out = append(out, t)
	}
	return out, nil
}

func (a *Adapter) BackstopInterval() time.Duration { return 2 * time.Second }
