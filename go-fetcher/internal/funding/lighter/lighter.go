// Package lighter — funding adapter for Lighter ZK perp-DEX.
//
// Two concurrent REST calls joined by symbol:
//   GET https://mainnet.zklighter.elliot.ai/api/v1/funding-rates
//     → {"funding_rates":[{exchange, symbol, rate}]}
//     filter exchange=="lighter"
//   GET https://mainnet.zklighter.elliot.ai/api/v1/exchangeStats
//     → {"order_book_stats":[{symbol, last_trade_price, daily_quote_token_volume}]}
//
// Funding interval is 1h (confirmed via /api/v1/fundings — timestamps are
// 3600s apart). Next-funding boundary = top of next hour UTC.
// Rows with rate==0 are skipped (cross-venue reference rates also appear
// in the funding-rates response; the exchange filter handles most but
// zero-rate entries are noise).
package lighter

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
	fundingRatesURL = "https://mainnet.zklighter.elliot.ai/api/v1/funding-rates"
	exchangeStatsURL = "https://mainnet.zklighter.elliot.ai/api/v1/exchangeStats"
)

type Adapter struct{}

func New() *Adapter { return &Adapter{} }

func (a *Adapter) Name() string                             { return "lighter" }
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
	var frRes, stRes result
	var wg sync.WaitGroup
	wg.Add(2)
	go func() { defer wg.Done(); b, e := getJSON(ctx, fundingRatesURL); frRes = result{b, e} }()
	go func() { defer wg.Done(); b, e := getJSON(ctx, exchangeStatsURL); stRes = result{b, e} }()
	wg.Wait()

	if frRes.err != nil {
		return nil, frRes.err
	}
	if stRes.err != nil {
		return nil, stRes.err
	}

	// funding-rates: filter exchange=="lighter"
	var frDoc struct {
		FundingRates []struct {
			Exchange string  `json:"exchange"`
			Symbol   string  `json:"symbol"`
			Rate     float64 `json:"rate"`
		} `json:"funding_rates"`
	}
	if err := sonic.Unmarshal(frRes.body, &frDoc); err != nil {
		return nil, err
	}
	rateBySymbol := make(map[string]float64, len(frDoc.FundingRates))
	for _, r := range frDoc.FundingRates {
		if !strings.EqualFold(r.Exchange, "lighter") {
			continue
		}
		sym := strings.ToUpper(r.Symbol)
		if sym != "" && r.Rate != 0 {
			rateBySymbol[sym] = r.Rate
		}
	}

	// exchangeStats: last_trade_price + daily_quote_token_volume
	var stDoc struct {
		OrderBookStats []struct {
			Symbol                 string  `json:"symbol"`
			LastTradePrice         float64 `json:"last_trade_price"`
			DailyQuoteTokenVolume  float64 `json:"daily_quote_token_volume"`
		} `json:"order_book_stats"`
	}
	if err := sonic.Unmarshal(stRes.body, &stDoc); err != nil {
		return nil, err
	}

	now := time.Now().Unix()
	nextFunding := time.Unix((now/3600+1)*3600, 0)

	out := make([]funding.Tick, 0, len(stDoc.OrderBookStats))
	for _, s := range stDoc.OrderBookStats {
		sym := strings.ToUpper(s.Symbol)
		rate, ok := rateBySymbol[sym]
		if !ok {
			continue
		}
		if s.LastTradePrice <= 0 {
			continue
		}
		out = append(out, funding.Tick{
			Symbol:      sym,
			Rate:        rate,
			MarkPrice:   s.LastTradePrice,
			Volume24h:   s.DailyQuoteTokenVolume,
			NextFunding: nextFunding,
			IntervalH:   1.0,
		})
	}
	if len(out) == 0 {
		return nil, errors.New("lighter: empty results")
	}
	return out, nil
}

// 5min → 10s: 2 parallel bulk calls (funding-rates + exchangeStats), no
// per-symbol expansion. mainnet.zklighter.elliot.ai stays well within
// limits at 6 req/min total. Funding freshness from up-to-5min to <15s.
func (a *Adapter) BackstopInterval() time.Duration { return 10 * time.Second }

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
