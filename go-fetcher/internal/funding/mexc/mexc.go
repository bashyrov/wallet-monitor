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
//
// Funding interval is per-symbol (4h or 8h). Fetched from
// /api/v1/contract/detail and cached.
package mexc

import (
	"context"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding"
)

const restURL = "https://contract.mexc.com/api/v1/contract/ticker"

type Adapter struct {
	fundMu       sync.RWMutex
	fundInterval map[string]int // "BTCUSDT" -> hours
}

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
	// Populate the per-symbol funding-interval cache.
	a.fetchIntervalCache(ctx)

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
	// next interval boundary in UTC — MEXC settles at 00:00/08:00/16:00
	// UTC for all USDT-perp contracts. Without this every MEXC row had
	// next_ts=0 and the screener "next funding" column was empty.
	cycle := time.Duration(8) * time.Hour
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
		ivl := a.lookupInterval(r.Symbol)
		t := funding.Tick{
			Symbol:      token,
			Rate:        r.FundingRate,
			MarkPrice:   mark,
			IndexPrice:  r.IndexPrice,
			Volume24h:   r.Amount24,
			IntervalH:   float64(ivl),
			NextFunding: nextSettle,
		}
		if r.NextSettleTime > 0 {
			t.NextFunding = time.UnixMilli(r.NextSettleTime)
		}
		out = append(out, t)
	}
	return out, nil
}

// fetchIntervalCache fetches /contract/detail once and populates
// a.fundInterval. Called once per BackstopFetch cycle.
func (a *Adapter) fetchIntervalCache(ctx context.Context) {
	a.fundMu.RLock()
	if a.fundInterval != nil {
		a.fundMu.RUnlock()
		return
	}
	a.fundMu.RUnlock()

	var doc struct {
		Code int `json:"code"`
		Data []struct {
			Symbol             string `json:"symbol"`
			FundingIntervalHrs int    `json:"fundingIntervalHours"`
		} `json:"data"`
	}
	if err := funding.HTTPGet(ctx, "https://contract.mexc.com/api/v1/contract/detail", &doc); err != nil {
		return
	}
	a.fundMu.Lock()
	defer a.fundMu.Unlock()
	a.fundInterval = make(map[string]int, len(doc.Data))
	for _, d := range doc.Data {
		if d.FundingIntervalHrs < 1 {
			d.FundingIntervalHrs = 8
		}
		a.fundInterval[d.Symbol] = d.FundingIntervalHrs
	}
}

// lookupInterval returns the per-symbol funding interval in hours.
// Falls back to 8h if the symbol isn't cached.
func (a *Adapter) lookupInterval(symbol string) int {
	a.fundMu.RLock()
	defer a.fundMu.RUnlock()
	if ivl, ok := a.fundInterval[symbol]; ok {
		return ivl
	}
	return 8
}

// MEXC returns ALL contracts in one shot; no point hitting it more than
// every 3-4s. Slightly higher than the others to keep load low.
func (a *Adapter) BackstopInterval() time.Duration { return 3 * time.Second }
