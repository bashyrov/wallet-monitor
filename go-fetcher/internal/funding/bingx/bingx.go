// Package bingx — funding adapter for BingX swap.
//
// BingX caps WS at ~100 symbols per connection (same as orderbook bug
// #5 territory), and our Python adapter found the funding feed less
// reliable than REST. We use REST-only here; BingX's premiumIndex
// endpoint returns rate + nextFundingTime for every symbol in one call.
//
// Per-pair interval — BingX has no batch endpoint that reports the
// funding interval. We populate it lazily by fetching the last two
// funding records for each symbol via /openApi/swap/v2/quote/fundingRate
// and computing the spacing in hours. Background-refreshed; cached
// 24 h since intervals never change.
package bingx

import (
	"context"
	"math"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
)

// URLs — vars (not const) so package tests can override.
var (
	restURL    = "https://open-api.bingx.com/openApi/swap/v2/quote/premiumIndex"
	tickerURL  = "https://open-api.bingx.com/openApi/swap/v2/quote/ticker"
	fundingURL = "https://open-api.bingx.com/openApi/swap/v2/quote/fundingRate"
)

type Adapter struct {
	// Per-symbol funding interval cache. Default 8h until the lazy
	// background refresh fills the actual value (which can be 4h on
	// some pairs — without this, the screener used to flash 8h for
	// every BingX row regardless of reality).
	intervalMu sync.RWMutex
	interval   map[string]float64

	// Set of symbols we've already kicked off a fetch for, so we
	// don't dogpile the funding-history endpoint with repeats from
	// every backstop tick.
	pendingMu sync.Mutex
	pending   map[string]bool
	lastSweep time.Time
}

const (
	intervalCacheTTL = 24 * time.Hour
	// How often we refresh the per-symbol intervals. Every 4h is plenty
	// — exchanges almost never change a pair's funding cadence, but
	// we want SOME refresh so newly-listed pairs get picked up.
	intervalSweepInterval = 4 * time.Hour
	// At each sweep, only fetch this many fresh symbols (rate-limit
	// safe — BingX caps history calls). Top-volume pairs get
	// prioritised; the long tail catches up on subsequent sweeps.
	intervalSweepBatch = 24
)

func New() *Adapter {
	return &Adapter{
		interval: make(map[string]float64, 256),
		pending:  make(map[string]bool, 256),
	}
}

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
			Symbol          string `json:"symbol"`
			MarkPrice       string `json:"markPrice"`
			IndexPrice      string `json:"indexPrice"`
			LastFundingRate string `json:"lastFundingRate"`
			NextFundingTime int64  `json:"nextFundingTime"`
		} `json:"data"`
	}
	if err := funding.HTTPGet(ctx, restURL, &doc); err != nil {
		return nil, err
	}

	// Volume in parallel; non-fatal on failure.
	volBySymbol := make(map[string]float64, len(doc.Data))
	var tdoc struct {
		Data []struct {
			Symbol      string `json:"symbol"`
			QuoteVolume string `json:"quoteVolume"`
		} `json:"data"`
	}
	if err := funding.HTTPGet(ctx, tickerURL, &tdoc); err == nil {
		for _, r := range tdoc.Data {
			if !strings.HasSuffix(r.Symbol, "-USDT") {
				continue
			}
			token := strings.TrimSuffix(r.Symbol, "-USDT")
			vol, _ := strconv.ParseFloat(r.QuoteVolume, 64)
			if vol > 0 {
				volBySymbol[token] = vol
			}
		}
	}

	// Kick off a sweep of the per-symbol interval cache if we're due.
	// Runs in a goroutine so the backstop doesn't block on N×HTTP calls.
	a.maybeStartIntervalSweep(ctx, doc.Data, volBySymbol)

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
			Symbol:     token,
			Rate:       rate,
			MarkPrice:  mark,
			IndexPrice: idx,
			Volume24h:  volBySymbol[token],
			IntervalH:  a.lookupInterval(token),
		}
		if r.NextFundingTime > 0 {
			t.NextFunding = time.UnixMilli(r.NextFundingTime)
		}
		out = append(out, t)
	}
	return out, nil
}

func (a *Adapter) BackstopInterval() time.Duration { return 2 * time.Second }

// ── Per-symbol interval cache ────────────────────────────────────────────

// lookupInterval returns the cached interval for `token` (BTC, BSB, …)
// or 0 if we haven't fetched it yet. The store treats 0 as "no value"
// and preserves the last non-zero value, so a freshly-listed symbol
// shows the dumper's 8h fallback until the sweep populates a real value.
func (a *Adapter) lookupInterval(token string) float64 {
	a.intervalMu.RLock()
	defer a.intervalMu.RUnlock()
	return a.interval[token]
}

// maybeStartIntervalSweep — fires at most once per `intervalSweepInterval`.
// Picks the top-N symbols by volume that don't yet have a cached
// interval AND aren't already in flight, fires a goroutine to fetch
// each one's funding history.
func (a *Adapter) maybeStartIntervalSweep(
	ctx context.Context,
	rows []struct {
		Symbol          string `json:"symbol"`
		MarkPrice       string `json:"markPrice"`
		IndexPrice      string `json:"indexPrice"`
		LastFundingRate string `json:"lastFundingRate"`
		NextFundingTime int64  `json:"nextFundingTime"`
	},
	volBySymbol map[string]float64,
) {
	a.pendingMu.Lock()
	if !a.lastSweep.IsZero() && time.Since(a.lastSweep) < intervalSweepInterval {
		a.pendingMu.Unlock()
		return
	}
	a.lastSweep = time.Now()
	a.pendingMu.Unlock()

	// Build list of (token, volume) for symbols we don't have yet.
	type cand struct {
		token string
		vol   float64
	}
	cands := make([]cand, 0, len(rows))
	for _, r := range rows {
		if !strings.HasSuffix(r.Symbol, "-USDT") {
			continue
		}
		token := strings.TrimSuffix(r.Symbol, "-USDT")
		if a.lookupInterval(token) > 0 {
			continue
		}
		cands = append(cands, cand{token: token, vol: volBySymbol[token]})
	}
	sort.Slice(cands, func(i, j int) bool { return cands[i].vol > cands[j].vol })
	if len(cands) > intervalSweepBatch {
		cands = cands[:intervalSweepBatch]
	}

	for _, c := range cands {
		c := c
		a.pendingMu.Lock()
		if a.pending[c.token] {
			a.pendingMu.Unlock()
			continue
		}
		a.pending[c.token] = true
		a.pendingMu.Unlock()

		go func() {
			defer func() {
				a.pendingMu.Lock()
				delete(a.pending, c.token)
				a.pendingMu.Unlock()
			}()
			ih := fetchFundingInterval(ctx, c.token)
			if ih > 0 {
				a.intervalMu.Lock()
				a.interval[c.token] = ih
				a.intervalMu.Unlock()
				log.L().Debug().Str("ex", "bingx").Str("sym", c.token).
					Float64("interval_h", ih).Msg("funding interval discovered")
			}
		}()
	}
}

// fetchFundingInterval returns the per-pair funding interval in hours
// by computing the spacing between the last two funding events. Returns
// 0 on any failure — caller treats 0 as "unknown, leave default".
func fetchFundingInterval(ctx context.Context, token string) float64 {
	u := fundingURL + "?" + url.Values{
		"symbol": {token + "-USDT"},
		"limit":  {"3"},
	}.Encode()
	var doc struct {
		Data []struct {
			Symbol      string `json:"symbol"`
			FundingTime int64  `json:"fundingTime"`
		} `json:"data"`
	}
	if err := funding.HTTPGet(ctx, u, &doc); err != nil {
		return 0
	}
	if len(doc.Data) < 2 {
		return 0
	}
	// Most-recent first. Spacing = ms between consecutive timestamps.
	delta := doc.Data[0].FundingTime - doc.Data[1].FundingTime
	if delta < 0 {
		delta = doc.Data[1].FundingTime - doc.Data[0].FundingTime
	}
	if delta <= 0 {
		return 0
	}
	hours := float64(delta) / 3600000.0
	// Snap to the typical 1/2/4/8h grid — BingX always uses one of
	// these values; any small deviation is timing jitter.
	for _, std := range []float64{1, 2, 4, 8} {
		if math.Abs(hours-std) < 0.5 {
			return std
		}
	}
	return hours
}
