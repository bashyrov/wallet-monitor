// Package whitebit — funding adapter for WhiteBIT perp.
//
// REST-only: /api/v4/public/futures returns each perp market with the
// funding_rate, last_price, index_price, money_volume (USDT), open
// interest, and next_funding_rate_timestamp fields. Probe-confirmed —
// /api/v4/public/markets does NOT carry funding rate; we needed the
// futures-specific endpoint.
//
// REST: https://whitebit.com/api/v4/public/futures
package whitebit

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding"
)

// restURL — var (not const) so package tests can override.
var restURL = "https://whitebit.com/api/v4/public/futures"

type Adapter struct{}

func New() *Adapter { return &Adapter{} }

func (a *Adapter) Name() string                          { return "whitebit" }
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
		Result []struct {
			TickerID                 string `json:"ticker_id"`        // "BTC_PERP"
			LastPrice                string `json:"last_price"`
			IndexPrice               string `json:"index_price"`
			FundingRate              string `json:"funding_rate"`
			NextFundingRateTimestamp string `json:"next_funding_rate_timestamp"`
			MoneyVolume              string `json:"money_volume"`
			OpenInterest             string `json:"open_interest"`
		} `json:"result"`
	}
	if err := funding.HTTPGet(ctx, restURL, &doc); err != nil {
		return nil, err
	}
	out := make([]funding.Tick, 0, len(doc.Result))
	for _, r := range doc.Result {
		if !strings.HasSuffix(r.TickerID, "_PERP") {
			continue
		}
		token := strings.TrimSuffix(r.TickerID, "_PERP")
		rate, _ := strconv.ParseFloat(r.FundingRate, 64)
		last, _ := strconv.ParseFloat(r.LastPrice, 64)
		idx, _ := strconv.ParseFloat(r.IndexPrice, 64)
		vol, _ := strconv.ParseFloat(r.MoneyVolume, 64)
		oi, _ := strconv.ParseFloat(r.OpenInterest, 64)
		nextMs, _ := strconv.ParseInt(r.NextFundingRateTimestamp, 10, 64)
		t := funding.Tick{
			Symbol:     token,
			Rate:       rate,
			MarkPrice:  last,
			IndexPrice: idx,
			Volume24h:  vol,
			OpenIntUSD: oi * last,
			IntervalH:  8,
		}
		if nextMs > 0 {
			t.NextFunding = time.UnixMilli(nextMs)
		}
		out = append(out, t)
	}
	return out, nil
}

func (a *Adapter) BackstopInterval() time.Duration { return 3 * time.Second }
