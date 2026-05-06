// Package kraken — funding adapter for Kraken Futures linear perps.
//
// REST: GET https://futures.kraken.com/derivatives/api/v3/tickers
// returns {"tickers":[{symbol, markPrice, fundingRate, volumeQuote, ...}]}.
//
// Only PF_*USD symbols (linear USD-collateralised perps) are kept.
// PI_ (inverse) and multi-collateral contracts are ignored.
// XBT is normalised to BTC so cross-join with other venues works.
// Funding interval is 1h; next-funding boundary = top of next hour UTC.
package kraken

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/bytedance/sonic"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding"
)

const restURL = "https://futures.kraken.com/derivatives/api/v3/tickers"

type Adapter struct{}

func New() *Adapter { return &Adapter{} }

func (a *Adapter) Name() string                             { return "kraken" }
func (a *Adapter) URL(_ context.Context) (string, error)   { return "", nil }
func (a *Adapter) BuildSubscribe(_ []string) [][]byte      { return nil }
func (a *Adapter) ParseWS(_ []byte) ([]funding.Tick, error) { return nil, nil }
func (a *Adapter) Heartbeat() []byte                       { return nil }
func (a *Adapter) HeartbeatInterval() time.Duration        { return 0 }
func (a *Adapter) PongFor(_ []byte) []byte                 { return nil }
func (a *Adapter) UseLibPings() bool                       { return false }
func (a *Adapter) DecompressGzip() bool                    { return false }

func (a *Adapter) BackstopFetch(ctx context.Context, _ []string) ([]funding.Tick, error) {
	body, err := getJSON(ctx, restURL)
	if err != nil {
		return nil, err
	}

	var doc struct {
		Tickers []struct {
			Symbol      string  `json:"symbol"`
			Suspended   bool    `json:"suspended"`
			MarkPrice   float64 `json:"markPrice"`
			Last        float64 `json:"last"`
			FundingRate float64 `json:"fundingRate"`
			VolumeQuote float64 `json:"volumeQuote"`
			Vol24h      float64 `json:"vol24h"`
		} `json:"tickers"`
	}
	if err := sonic.Unmarshal(body, &doc); err != nil {
		return nil, err
	}

	now := time.Now().Unix()
	nextFunding := time.Unix((now/3600+1)*3600, 0)

	out := make([]funding.Tick, 0, len(doc.Tickers))
	for _, t := range doc.Tickers {
		if t.Suspended {
			continue
		}
		sym := t.Symbol
		if !strings.HasPrefix(sym, "PF_") || !strings.HasSuffix(sym, "USD") {
			continue
		}
		token := sym[len("PF_") : len(sym)-len("USD")]
		if token == "" {
			continue
		}
		if token == "XBT" {
			token = "BTC"
		}
		price := t.MarkPrice
		if price == 0 {
			price = t.Last
		}
		if price <= 0 || t.FundingRate == 0 {
			continue
		}
		vol := t.VolumeQuote
		if vol == 0 {
			vol = t.Vol24h
		}
		out = append(out, funding.Tick{
			Symbol:      token,
			Rate:        t.FundingRate,
			MarkPrice:   price,
			Volume24h:   vol,
			NextFunding: nextFunding,
			IntervalH:   1.0,
		})
	}
	if len(out) == 0 {
		return nil, errors.New("kraken: empty results")
	}
	return out, nil
}

func (a *Adapter) BackstopInterval() time.Duration { return 5 * time.Minute }

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
