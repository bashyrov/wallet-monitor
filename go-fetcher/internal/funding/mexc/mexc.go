// Package mexc — funding adapter for MEXC contract.
//
// MEXC's WS funding feed is unreliable for our use case (no continuous
// rate push, only on-settle); we use REST-only here. The Python adapter
// also fell back to REST for funding rate (separate from ticker WS).
//
// REST: https://contract.mexc.com/api/v1/contract/ticker (mark/last/vol)
//   merged with /api/v1/contract/funding_rate (rate per symbol — slow).
// To stay fast we use ONLY the ticker endpoint (it includes fundingRate
// and nextSettleTime).
package mexc

import (
	"context"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding"
)

const restURL = "https://contract.mexc.com/api/v1/contract/ticker"

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
	var doc struct {
		Data []struct {
			Symbol         string  `json:"symbol"`
			LastPrice      float64 `json:"lastPrice"`
			IndexPrice     float64 `json:"indexPrice"`
			FairPrice      float64 `json:"fairPrice"` // mark equivalent
			FundingRate    float64 `json:"fundingRate"`
			NextSettleTime int64   `json:"nextSettleTime"`
			Amount24       float64 `json:"amount24"`
		} `json:"data"`
	}
	if err := funding.HTTPGet(ctx, restURL, &doc); err != nil {
		return nil, err
	}
	// MEXC's bulk ticker endpoint omits `nextSettleTime` (only the per-
	// symbol /contract/funding_rate/<symbol> route carries it). 885
	// per-symbol fetches don't fit the per-call budget, so compute the
	// next 8h boundary in UTC — MEXC settles at 00:00/08:00/16:00 UTC
	// for all USDT-perp contracts. Without this every MEXC row had
	// next_ts=0 and the screener "next funding" column was empty.
	const cyclehrs = 8
	cycle := time.Duration(cyclehrs) * time.Hour
	nextSettle := time.Now().UTC().Truncate(cycle).Add(cycle)

	out := make([]funding.Tick, 0, len(doc.Data))
	for _, r := range doc.Data {
		if !strings.HasSuffix(r.Symbol, "_USDT") {
			continue
		}
		token := strings.TrimSuffix(r.Symbol, "_USDT")
		mark := r.FairPrice
		if mark == 0 {
			mark = r.LastPrice
		}
		t := funding.Tick{
			Symbol:      token,
			Rate:        r.FundingRate,
			MarkPrice:   mark,
			IndexPrice:  r.IndexPrice,
			Volume24h:   r.Amount24,
			IntervalH:   cyclehrs,
			NextFunding: nextSettle,
		}
		if r.NextSettleTime > 0 {
			t.NextFunding = time.UnixMilli(r.NextSettleTime)
		}
		out = append(out, t)
	}
	return out, nil
}

// MEXC returns ALL contracts in one shot; no point hitting it more than
// every 3-4s. Slightly higher than the others to keep load low.
func (a *Adapter) BackstopInterval() time.Duration { return 3 * time.Second }
