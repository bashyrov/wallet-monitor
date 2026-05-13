// Package htx — funding adapter for HTX (Huobi) USDT-margined linear swap.
//
// REST-only: WS funding-rate channel is sparse (publishes on settle); the
// REST endpoint returns all symbols' funding rates in one shot.
//
// REST: https://api.hbdm.com/linear-swap-api/v1/swap_batch_funding_rate
package htx

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding"
)

// URLs — vars (not const) so package tests can override.
// batch_merged returns 24h vol+price for every USDT-margined contract.
var (
	restURL   = "https://api.hbdm.com/linear-swap-api/v1/swap_batch_funding_rate"
	tickerURL = "https://api.hbdm.com/linear-swap-ex/market/detail/batch_merged?business_type=swap"
)

type Adapter struct{}

func New() *Adapter { return &Adapter{} }

func (a *Adapter) Name() string                          { return "htx" }
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
			ContractCode    string `json:"contract_code"` // "BTC-USDT"
			FundingRate     string `json:"funding_rate"`
			NextFundingRate string `json:"estimated_rate"`
			FundingTime     string `json:"funding_time"`
			NextFundingTime string `json:"next_funding_time"`
			FundingPeriod   int64  `json:"funding_period"` // hours
		} `json:"data"`
	}
	if err := funding.HTTPGet(ctx, restURL, &doc); err != nil {
		return nil, err
	}

	// Volume + mark from batch_merged (USDT vol + close price).
	volBySymbol := make(map[string]float64, len(doc.Data))
	markBySymbol := make(map[string]float64, len(doc.Data))
	var tdoc struct {
		Ticks []struct {
			ContractCode string  `json:"contract_code"`
			Close        float64 `json:"close"`
			Vol          float64 `json:"vol"`
			Amount       float64 `json:"amount"` // base units
			TradeTurnover float64 `json:"trade_turnover"` // USDT turnover
		} `json:"ticks"`
	}
	if err := funding.HTTPGet(ctx, tickerURL, &tdoc); err == nil {
		for _, r := range tdoc.Ticks {
			if !strings.HasSuffix(r.ContractCode, "-USDT") {
				continue
			}
			token := strings.TrimSuffix(r.ContractCode, "-USDT")
			turnover := r.TradeTurnover
			if turnover <= 0 {
				// Fallback: amount × close (USDT) as approximation.
				turnover = r.Amount * r.Close
			}
			if turnover > 0 {
				volBySymbol[token] = turnover
			}
			if r.Close > 0 {
				markBySymbol[token] = r.Close
			}
		}
	}

	out := make([]funding.Tick, 0, len(doc.Data))
	for _, r := range doc.Data {
		if !strings.HasSuffix(r.ContractCode, "-USDT") {
			continue
		}
		token := strings.TrimSuffix(r.ContractCode, "-USDT")
		rate, _ := strconv.ParseFloat(r.FundingRate, 64)
		nextMs, _ := strconv.ParseInt(r.NextFundingTime, 10, 64)
		intH := float64(r.FundingPeriod)
		if intH <= 0 {
			intH = 8
		}
		t := funding.Tick{
			Symbol:    token,
			Rate:      rate,
			MarkPrice: markBySymbol[token],
			Volume24h: volBySymbol[token],
			IntervalH: intH,
		}
		if nextMs > 0 {
			t.NextFunding = time.UnixMilli(nextMs)
		}
		out = append(out, t)
	}
	return out, nil
}

func (a *Adapter) BackstopInterval() time.Duration { return 3 * time.Second }
