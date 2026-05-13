package backpack

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
)

// Backpack hits 3 endpoints in parallel (markets, markPrices, tickers).
func withTestRESTs(t *testing.T, markets, markPrices, tickers http.HandlerFunc) {
	t.Helper()
	msrv := httptest.NewServer(markets)
	psrv := httptest.NewServer(markPrices)
	tsrv := httptest.NewServer(tickers)
	t.Cleanup(msrv.Close)
	t.Cleanup(psrv.Close)
	t.Cleanup(tsrv.Close)
	origM, origMP, origT := marketsURL, markPricesURL, tickersURL
	marketsURL = msrv.URL
	markPricesURL = psrv.URL
	tickersURL = tsrv.URL
	t.Cleanup(func() {
		marketsURL = origM
		markPricesURL = origMP
		tickersURL = origT
	})
}

func TestBackstopFetch_DecodesAllThreeFeeds(t *testing.T) {
	withTestRESTs(t,
		func(w http.ResponseWriter, r *http.Request) {
			// markets — fundingInterval in ms (3600000 = 1h)
			_, _ = w.Write([]byte(`[
				{"symbol":"BTC_USDC_PERP","marketType":"PERP","fundingInterval":3600000}
			]`))
		},
		func(w http.ResponseWriter, r *http.Request) {
			// markPrices — strings
			_, _ = w.Write([]byte(`[
				{"symbol":"BTC_USDC_PERP","markPrice":"60000","fundingRate":"0.00001","nextFundingTimestamp":1718000028000}
			]`))
		},
		func(w http.ResponseWriter, r *http.Request) {
			// tickers
			_, _ = w.Write([]byte(`[
				{"symbol":"BTC_USDC_PERP","quoteVolume":"1000000"}
			]`))
		},
	)
	ticks, err := New().BackstopFetch(context.Background(), nil)
	if err != nil {
		t.Fatalf("BackstopFetch: %v", err)
	}
	if len(ticks) != 1 || ticks[0].Symbol != "BTC" {
		t.Errorf("decoded: %+v", ticks)
	}
	if ticks[0].IntervalH != 1 {
		t.Errorf("interval: 3600000ms=1h, got %v", ticks[0].IntervalH)
	}
	if ticks[0].Volume24h != 1e6 {
		t.Errorf("volume joined: %v", ticks[0].Volume24h)
	}
}

func TestBackstopFetch_SkipsNonPERPMarkets(t *testing.T) {
	withTestRESTs(t,
		func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`[
				{"symbol":"BTC_USDC","marketType":"SPOT","fundingInterval":0},
				{"symbol":"BTC_USDC_PERP","marketType":"PERP","fundingInterval":3600000}
			]`))
		},
		func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`[
				{"symbol":"BTC_USDC_PERP","markPrice":"60000","fundingRate":"0.00001"}
			]`))
		},
		func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`[]`))
		},
	)
	ticks, _ := New().BackstopFetch(context.Background(), nil)
	if len(ticks) != 1 || ticks[0].Symbol != "BTC" {
		t.Errorf("spot/PERP filter: %+v", ticks)
	}
}

func TestBackstopFetch_MarketsFailureErrors(t *testing.T) {
	withTestRESTs(t,
		func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusBadGateway)
		},
		func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`[]`))
		},
		func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`[]`))
		},
	)
	if _, err := New().BackstopFetch(context.Background(), nil); err == nil {
		t.Errorf("markets non-200 should error")
	}
}

func TestBackstopFetch_MarkPricesFailureErrors(t *testing.T) {
	withTestRESTs(t,
		func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`[]`))
		},
		func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusBadGateway)
		},
		func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`[]`))
		},
	)
	if _, err := New().BackstopFetch(context.Background(), nil); err == nil {
		t.Errorf("markPrices non-200 should error")
	}
}

func TestBackstopFetch_EmptyResultErrors(t *testing.T) {
	withTestRESTs(t,
		func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte(`[]`)) },
		func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte(`[]`)) },
		func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte(`[]`)) },
	)
	if _, err := New().BackstopFetch(context.Background(), nil); err == nil {
		t.Errorf("empty results should error")
	}
}
