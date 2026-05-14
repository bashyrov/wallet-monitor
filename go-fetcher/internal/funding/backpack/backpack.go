// Package backpack — funding adapter for Backpack perp-DEX.
//
// Three concurrent REST calls joined by symbol:
//   GET https://api.backpack.exchange/api/v1/markets
//     → fundingInterval (ms) per PERP market
//   GET https://api.backpack.exchange/api/v1/markPrices
//     → fundingRate, markPrice, nextFundingTimestamp (ms) per _USDC_PERP symbol
//   GET https://api.backpack.exchange/api/v1/tickers
//     → quoteVolume (USDC ≈ USD) per _USDC_PERP symbol
//
// Symbol convention: <BASE>_USDC_PERP — strip suffix to get base token.
// Funding interval varies per market (1h default if absent).
// nextFundingTimestamp is ms since epoch; divide by 1000 for unix seconds.
package backpack

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/bytedance/sonic"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding"
)

const (
	marketsURL    = "https://api.backpack.exchange/api/v1/markets"
	markPricesURL = "https://api.backpack.exchange/api/v1/markPrices"
	tickersURL    = "https://api.backpack.exchange/api/v1/tickers"
)

type Adapter struct{}

func New() *Adapter { return &Adapter{} }

func (a *Adapter) Name() string                             { return "backpack" }
func (a *Adapter) URL(_ context.Context) (string, error)   { return "", nil }
func (a *Adapter) BuildSubscribe(_ []string) [][]byte      { return nil }
func (a *Adapter) ParseWS(_ []byte) ([]funding.Tick, error) { return nil, nil }
func (a *Adapter) Heartbeat() []byte                       { return nil }
func (a *Adapter) HeartbeatInterval() time.Duration        { return 0 }
func (a *Adapter) PongFor(_ []byte) []byte                 { return nil }
func (a *Adapter) UseLibPings() bool                       { return false }
func (a *Adapter) DecompressGzip() bool                    { return false }

func (a *Adapter) BackstopFetch(ctx context.Context, _ []string) ([]funding.Tick, error) {
	type result struct {
		body []byte
		err  error
	}
	fetch := func(url string) result {
		b, e := getJSON(ctx, url)
		return result{b, e}
	}

	var (
		mrRes, mpRes, tkRes result
		wg                  sync.WaitGroup
	)
	wg.Add(3)
	go func() { defer wg.Done(); mrRes = fetch(marketsURL) }()
	go func() { defer wg.Done(); mpRes = fetch(markPricesURL) }()
	go func() { defer wg.Done(); tkRes = fetch(tickersURL) }()
	wg.Wait()

	if mrRes.err != nil {
		return nil, mrRes.err
	}
	if mpRes.err != nil {
		return nil, mpRes.err
	}
	if tkRes.err != nil {
		return nil, tkRes.err
	}

	// markets → fundingInterval_h per symbol
	var markets []struct {
		Symbol          string  `json:"symbol"`
		MarketType      string  `json:"marketType"`
		FundingInterval float64 `json:"fundingInterval"` // ms
	}
	if err := sonic.Unmarshal(mrRes.body, &markets); err != nil {
		return nil, err
	}
	ivlH := make(map[string]float64, len(markets))
	for _, m := range markets {
		if m.MarketType != "PERP" || m.Symbol == "" {
			continue
		}
		h := 1.0
		if m.FundingInterval > 0 {
			h = m.FundingInterval / 3_600_000.0
			if h < 1 {
				h = 1
			}
		}
		ivlH[m.Symbol] = h
	}

	// tickers → quoteVolume per _USDC_PERP symbol (returned as string by API)
	var tickers []struct {
		Symbol      string      `json:"symbol"`
		QuoteVolume interface{} `json:"quoteVolume"`
	}
	if err := sonic.Unmarshal(tkRes.body, &tickers); err != nil {
		return nil, err
	}
	volBySymbol := make(map[string]float64, len(tickers))
	for _, t := range tickers {
		if strings.HasSuffix(t.Symbol, "_USDC_PERP") {
			volBySymbol[t.Symbol] = funding.ParseFloat(t.QuoteVolume)
		}
	}

	// markPrices → rate, price, nextFundingTimestamp (rate/price returned as strings)
	var markPrices []struct {
		Symbol               string      `json:"symbol"`
		MarkPrice            interface{} `json:"markPrice"`
		FundingRate          interface{} `json:"fundingRate"`
		NextFundingTimestamp int64       `json:"nextFundingTimestamp"` // ms, numeric
	}
	if err := sonic.Unmarshal(mpRes.body, &markPrices); err != nil {
		return nil, err
	}

	const suffix = "_USDC_PERP"
	out := make([]funding.Tick, 0, len(markPrices))
	for _, mp := range markPrices {
		if !strings.HasSuffix(mp.Symbol, suffix) {
			continue
		}
		base := strings.TrimSuffix(mp.Symbol, suffix)
		if base == "" {
			continue
		}
		if _, ok := ivlH[mp.Symbol]; !ok {
			continue // not a recognised PERP market
		}
		price := funding.ParseFloat(mp.MarkPrice)
		rate := funding.ParseFloat(mp.FundingRate)
		if price <= 0 || rate == 0 {
			continue
		}
		var nextFunding time.Time
		if mp.NextFundingTimestamp > 0 {
			nextFunding = time.Unix(mp.NextFundingTimestamp/1000, 0)
		}
		out = append(out, funding.Tick{
			Symbol:      base,
			Rate:        rate,
			MarkPrice:   price,
			Volume24h:   volBySymbol[mp.Symbol],
			NextFunding: nextFunding,
			IntervalH:   ivlH[mp.Symbol],
		})
	}
	if len(out) == 0 {
		return nil, errors.New("backpack: empty results")
	}
	return out, nil
}

// 5s — bulk endpoint, no per-symbol calls, Backpack rate-limit is loose
// enough. Was 5min — funding age was hitting 30-300s in the UI status
// dots which looked broken even though the data was technically valid.
func (a *Adapter) BackstopInterval() time.Duration { return 5 * time.Second }

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
