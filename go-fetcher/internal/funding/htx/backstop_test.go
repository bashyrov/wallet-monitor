package htx

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
)

// HTX backstop hits two endpoints — swap_batch_funding_rate and
// batch_merged (volume + close). withTestRESTs swaps both.
func withTestRESTs(t *testing.T, fundingHandler, tickersHandler http.HandlerFunc) {
	t.Helper()
	fundingSrv := httptest.NewServer(fundingHandler)
	tickersSrv := httptest.NewServer(tickersHandler)
	t.Cleanup(fundingSrv.Close)
	t.Cleanup(tickersSrv.Close)
	origREST, origTickers := restURL, tickerURL
	restURL = fundingSrv.URL
	tickerURL = tickersSrv.URL
	t.Cleanup(func() {
		restURL = origREST
		tickerURL = origTickers
	})
}

func TestBackstopFetch_DecodesFundingRate(t *testing.T) {
	withTestRESTs(t,
		func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`{"data":[
				{"contract_code":"BTC-USDT","funding_rate":"0.0001","next_funding_time":"1718000028000","funding_period":8}
			]}`))
		},
		func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`{"ticks":[
				{"contract_code":"BTC-USDT","close":60000,"trade_turnover":1000000}
			]}`))
		},
	)
	ticks, err := New().BackstopFetch(context.Background(), nil)
	if err != nil {
		t.Fatalf("BackstopFetch: %v", err)
	}
	if len(ticks) != 1 {
		t.Fatalf("len: %d", len(ticks))
	}
	if ticks[0].Symbol != "BTC" || ticks[0].Rate != 0.0001 {
		t.Errorf("decoded: %+v", ticks[0])
	}
	if ticks[0].Volume24h != 1e6 {
		t.Errorf("volume from batch_merged: %v", ticks[0].Volume24h)
	}
	if ticks[0].MarkPrice != 60000 {
		t.Errorf("mark from batch_merged close: %v", ticks[0].MarkPrice)
	}
	if ticks[0].IntervalH != 8 {
		t.Errorf("interval: %v", ticks[0].IntervalH)
	}
}

func TestBackstopFetch_TickersFailureNonFatal(t *testing.T) {
	withTestRESTs(t,
		func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`{"data":[
				{"contract_code":"BTC-USDT","funding_rate":"0.0001","next_funding_time":"1","funding_period":8}
			]}`))
		},
		func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusServiceUnavailable)
		},
	)
	// Should still return funding-rate ticks (vol/mark empty).
	ticks, err := New().BackstopFetch(context.Background(), nil)
	if err != nil {
		t.Fatalf("BackstopFetch: %v", err)
	}
	if len(ticks) != 1 || ticks[0].Volume24h != 0 {
		t.Errorf("tickers failure should leave vol=0: %+v", ticks)
	}
}

func TestBackstopFetch_FiltersNonUSDT(t *testing.T) {
	withTestRESTs(t,
		func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`{"data":[
				{"contract_code":"BTC-USDT","funding_rate":"0.0001","funding_period":8},
				{"contract_code":"BTC-USD","funding_rate":"0.0001","funding_period":8}
			]}`))
		},
		func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`{"ticks":[]}`))
		},
	)
	ticks, _ := New().BackstopFetch(context.Background(), nil)
	if len(ticks) != 1 {
		t.Errorf("non-USDT filtered: want 1 got %d", len(ticks))
	}
}

func TestBackstopFetch_FundingRateNon200Errors(t *testing.T) {
	withTestRESTs(t,
		func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusInternalServerError)
		},
		func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`{"ticks":[]}`))
		},
	)
	if _, err := New().BackstopFetch(context.Background(), nil); err == nil {
		t.Errorf("funding-rate non-200 should error")
	}
}
