package bingx

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
)

// BingX backstop hits 2 endpoints — premiumIndex + ticker. The third
// (fundingURL) is only used by background interval sweep.
func withTestRESTs(t *testing.T, premiumHandler, tickerHandler http.HandlerFunc) {
	t.Helper()
	psrv := httptest.NewServer(premiumHandler)
	tsrv := httptest.NewServer(tickerHandler)
	t.Cleanup(psrv.Close)
	t.Cleanup(tsrv.Close)
	origREST, origTicker := restURL, tickerURL
	restURL = psrv.URL
	tickerURL = tsrv.URL
	t.Cleanup(func() {
		restURL = origREST
		tickerURL = origTicker
	})
}

func TestBackstopFetch_DecodesPremiumIndex(t *testing.T) {
	withTestRESTs(t,
		func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`{"data":[
				{"symbol":"BTC-USDT","markPrice":"60000","indexPrice":"60000","lastFundingRate":"0.0001","nextFundingTime":1718000028000}
			]}`))
		},
		func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`{"data":[
				{"symbol":"BTC-USDT","quoteVolume":"1000000"}
			]}`))
		},
	)
	ticks, err := New().BackstopFetch(context.Background(), nil)
	if err != nil {
		t.Fatalf("BackstopFetch: %v", err)
	}
	if len(ticks) != 1 || ticks[0].Symbol != "BTC" {
		t.Errorf("decoded: %+v", ticks)
	}
	if ticks[0].Volume24h != 1e6 {
		t.Errorf("volume joined from ticker endpoint: %v", ticks[0].Volume24h)
	}
}

func TestBackstopFetch_TickerFailureNonFatal(t *testing.T) {
	withTestRESTs(t,
		func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`{"data":[
				{"symbol":"BTC-USDT","markPrice":"60000","lastFundingRate":"0.0001"}
			]}`))
		},
		func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusInternalServerError)
		},
	)
	ticks, err := New().BackstopFetch(context.Background(), nil)
	if err != nil {
		t.Fatalf("ticker failure should be non-fatal, got %v", err)
	}
	if len(ticks) != 1 || ticks[0].Volume24h != 0 {
		t.Errorf("vol should be 0 when ticker fails: %+v", ticks)
	}
}

func TestBackstopFetch_FiltersNonUSDT(t *testing.T) {
	withTestRESTs(t,
		func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`{"data":[
				{"symbol":"BTC-USDT","markPrice":"60000","lastFundingRate":"0.0001"},
				{"symbol":"BTC-USDC","markPrice":"60000","lastFundingRate":"0.0001"}
			]}`))
		},
		func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`{"data":[]}`))
		},
	)
	ticks, _ := New().BackstopFetch(context.Background(), nil)
	if len(ticks) != 1 {
		t.Errorf("non--USDT filtered: want 1 got %d", len(ticks))
	}
}

func TestBackstopFetch_PremiumIndexNon200Errors(t *testing.T) {
	withTestRESTs(t,
		func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusBadGateway)
		},
		func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`{"data":[]}`))
		},
	)
	if _, err := New().BackstopFetch(context.Background(), nil); err == nil {
		t.Errorf("premium-index non-200 should error")
	}
}
