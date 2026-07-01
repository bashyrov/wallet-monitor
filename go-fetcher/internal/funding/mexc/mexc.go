// Package mexc — funding adapter for MEXC contract.
//
// REST-only:
//   /api/v1/contract/funding_rate  (bulk) — per-symbol rate + collectCycle
//                                    (interval in hours) + nextSettleTime
//   /api/v1/contract/ticker        (bulk) — mark, index, volume
//
// Both endpoints return all 940+ pairs in one call. Prior version used
// /contract/detail hoping for `fundingIntervalHours` but that field
// doesn't exist on MEXC — every pair fell back to 8h and 96% of the
// top-liquidity 4h/1h pairs displayed the wrong APR.
package mexc

import (
	"context"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding"
)

const (
	restTickerURL      = "https://contract.mexc.com/api/v1/contract/ticker"
	restFundingRateURL = "https://contract.mexc.com/api/v1/contract/funding_rate"
)

type Adapter struct{}

func New() *Adapter { return &Adapter{} }

func (a *Adapter) Name() string                          { return "mexc" }
func (a *Adapter) URL(_ context.Context) (string, error) { return "", nil } // REST-only
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
	// 1. Bulk funding_rate — authoritative source for rate + interval + next.
	var fr struct {
		Data []struct {
			Symbol         string  `json:"symbol"`
			FundingRate    float64 `json:"fundingRate"`
			CollectCycle   int     `json:"collectCycle"`
			NextSettleTime int64   `json:"nextSettleTime"`
			IdxPrice       float64 `json:"idxPrice"`
			FairPrice      float64 `json:"fairPrice"`
		} `json:"data"`
	}
	if err := funding.HTTPGet(ctx, restFundingRateURL, &fr); err != nil {
		return nil, err
	}

	// 2. Bulk ticker — volume (funding_rate endpoint omits it).
	var tk struct {
		Data []struct {
			Symbol    string  `json:"symbol"`
			LastPrice float64 `json:"lastPrice"`
			Amount24  float64 `json:"amount24"`
		} `json:"data"`
	}
	volBySymbol := make(map[string]float64, len(fr.Data))
	lastBySymbol := make(map[string]float64, len(fr.Data))
	if err := funding.HTTPGet(ctx, restTickerURL, &tk); err == nil {
		for _, r := range tk.Data {
			if r.Amount24 > 0 {
				volBySymbol[r.Symbol] = r.Amount24
			}
			if r.LastPrice > 0 {
				lastBySymbol[r.Symbol] = r.LastPrice
			}
		}
	}

	out := make([]funding.Tick, 0, len(fr.Data))
	for _, r := range fr.Data {
		if !strings.HasSuffix(r.Symbol, "_USDT") {
			continue
		}
		token := strings.TrimSuffix(r.Symbol, "_USDT")
		mark := r.FairPrice
		if mark == 0 {
			mark = lastBySymbol[r.Symbol]
		}
		ivl := r.CollectCycle
		if ivl < 1 {
			ivl = 8
		}
		t := funding.Tick{
			Symbol:     token,
			Rate:       r.FundingRate,
			MarkPrice:  mark,
			IndexPrice: r.IdxPrice,
			Volume24h:  volBySymbol[r.Symbol],
			IntervalH:  float64(ivl),
		}
		if r.NextSettleTime > 0 {
			t.NextFunding = time.UnixMilli(r.NextSettleTime)
		} else {
			// Synthetic fallback — settle boundary from interval.
			cycle := time.Duration(ivl) * time.Hour
			t.NextFunding = time.Now().UTC().Truncate(cycle).Add(cycle)
		}
		out = append(out, t)
	}
	return out, nil
}

// MEXC returns ALL contracts in one shot; no point hitting it more than
// every 3-4s. Slightly higher than the others to keep load low.
func (a *Adapter) BackstopInterval() time.Duration { return 3 * time.Second }
