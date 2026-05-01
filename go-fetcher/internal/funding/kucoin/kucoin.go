// Package kucoin — funding adapter for KuCoin Futures.
//
// REST-only here: WS funding feed needs the same bullet-public token-auth
// flow as the orderbook adapter; we keep auth flow in orderbook package
// and stay simple on the funding side. /api/v1/contracts/active returns
// every USDTM contract with funding rate, mark price, volume.
//
// REST: https://api-futures.kucoin.com/api/v1/contracts/active
package kucoin

import (
	"context"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding"
)

const restURL = "https://api-futures.kucoin.com/api/v1/contracts/active"

type Adapter struct{}

func New() *Adapter { return &Adapter{} }

func (a *Adapter) Name() string                          { return "kucoin" }
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
			Symbol           string  `json:"symbol"`           // "XBTUSDTM"
			MarkPrice        float64 `json:"markPrice"`
			IndexPrice       float64 `json:"indexPrice"`
			FundingFeeRate   float64 `json:"fundingFeeRate"`
			NextFundingRateTime int64 `json:"nextFundingRateTime"`
			VolumeOf24h      float64 `json:"volumeOf24h"`
			TurnoverOf24h    float64 `json:"turnoverOf24h"`
			FundingRateInterval int  `json:"fundingRateInterval"` // minutes? hours?
		} `json:"data"`
	}
	if err := funding.HTTPGet(ctx, restURL, &doc); err != nil {
		return nil, err
	}
	out := make([]funding.Tick, 0, len(doc.Data))
	for _, r := range doc.Data {
		if !strings.HasSuffix(r.Symbol, "USDTM") {
			continue
		}
		token := strings.TrimSuffix(r.Symbol, "USDTM")
		if token == "XBT" {
			token = "BTC"
		}
		t := funding.Tick{
			Symbol:    token,
			Rate:      r.FundingFeeRate,
			MarkPrice: r.MarkPrice,
			IndexPrice: r.IndexPrice,
			Volume24h: r.TurnoverOf24h,
			IntervalH: 8,
		}
		if r.NextFundingRateTime > 0 {
			t.NextFunding = time.UnixMilli(r.NextFundingRateTime)
		}
		out = append(out, t)
	}
	return out, nil
}

func (a *Adapter) BackstopInterval() time.Duration { return 3 * time.Second }
