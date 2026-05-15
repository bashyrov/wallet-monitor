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

const (
	restURL   = "https://api.hbdm.com/linear-swap-api/v1/swap_batch_funding_rate"
	// batch_merged returns 24h vol+price for every USDT-margined contract.
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

	// Volume + mark from batch_merged. HTX wire returns those fields as
	// STRINGS in JSON ("close": "84.81", "vol": "23544", "trade_turnover":
	// "20095.391") — declaring them as float64 silently fails to decode
	// and every symbol ends up with 0 volume in funding.json (227/227
	// observed in prod 2026-05-13). Decode as string + ParseFloat to fix.
	volBySymbol := make(map[string]float64, len(doc.Data))
	markBySymbol := make(map[string]float64, len(doc.Data))
	// HTX batch_merged response uses either "ticks" or "data" as the root
	// array key depending on the API version/endpoint. Parse both.
	type tickItem struct {
		ContractCode  string `json:"contract_code"`
		Close         string `json:"close"`
		Vol           string `json:"vol"`            // base-unit contracts (primary)
		Amount        string `json:"amount"`         // alias seen in some responses
		TradeTurnover string `json:"trade_turnover"` // USDT turnover (preferred)
	}
	var tdoc struct {
		Ticks []tickItem `json:"ticks"`
		Data  []tickItem `json:"data"`
	}
	if err := funding.HTTPGet(ctx, tickerURL, &tdoc); err == nil {
		items := tdoc.Ticks
		if len(items) == 0 {
			items = tdoc.Data
		}
		for _, r := range items {
			if !strings.HasSuffix(r.ContractCode, "-USDT") {
				continue
			}
			token := strings.TrimSuffix(r.ContractCode, "-USDT")
			closePx, _ := strconv.ParseFloat(r.Close, 64)
			turnover, _ := strconv.ParseFloat(r.TradeTurnover, 64)
			if turnover <= 0 {
				// Fallback: vol (or amount) × close as USDT approximation.
				// API comment shows "vol":"23544" as the base-unit field name.
				amt, _ := strconv.ParseFloat(r.Vol, 64)
				if amt == 0 {
					amt, _ = strconv.ParseFloat(r.Amount, 64)
				}
				turnover = amt * closePx
			}
			if turnover > 0 {
				volBySymbol[token] = turnover
			}
			if closePx > 0 {
				markBySymbol[token] = closePx
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
		intH := float64(r.FundingPeriod)
		if intH <= 0 {
			intH = 8
		}
		// HTX returns `next_funding_time: null` for many rows and stores
		// the upcoming settlement in `funding_time` (which is documented
		// as "this funding's settlement time"). Prefer next_funding_time
		// when present, fall back to funding_time, then UTC 8h boundary.
		nextMs, _ := strconv.ParseInt(r.NextFundingTime, 10, 64)
		if nextMs <= 0 {
			nextMs, _ = strconv.ParseInt(r.FundingTime, 10, 64)
		}
		var nextT time.Time
		if nextMs > 0 {
			nextT = time.UnixMilli(nextMs)
		} else {
			cycle := time.Duration(intH) * time.Hour
			nextT = time.Now().UTC().Truncate(cycle).Add(cycle)
		}
		out = append(out, funding.Tick{
			Symbol:      token,
			Rate:        rate,
			MarkPrice:   markBySymbol[token],
			Volume24h:   volBySymbol[token],
			IntervalH:   intH,
			NextFunding: nextT,
		})
	}
	return out, nil
}

func (a *Adapter) BackstopInterval() time.Duration { return 3 * time.Second }
