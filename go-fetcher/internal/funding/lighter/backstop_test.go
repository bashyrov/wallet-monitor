package lighter

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
)

func withTestRESTs(t *testing.T, ratesHandler, statsHandler http.HandlerFunc) {
	t.Helper()
	rsrv := httptest.NewServer(ratesHandler)
	ssrv := httptest.NewServer(statsHandler)
	t.Cleanup(rsrv.Close)
	t.Cleanup(ssrv.Close)
	origR, origS := fundingRatesURL, exchangeStatsURL
	fundingRatesURL = rsrv.URL
	exchangeStatsURL = ssrv.URL
	t.Cleanup(func() {
		fundingRatesURL = origR
		exchangeStatsURL = origS
	})
}

func TestBackstopFetch_DecodesParallelFeeds(t *testing.T) {
	withTestRESTs(t,
		func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`{"funding_rates":[
				{"exchange":"lighter","symbol":"BTC","rate":0.00001}
			]}`))
		},
		func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`{"order_book_stats":[
				{"symbol":"BTC","last_trade_price":60000,"daily_quote_token_volume":1000000}
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
	if ticks[0].MarkPrice != 60000 || ticks[0].Volume24h != 1e6 {
		t.Errorf("joined: %+v", ticks[0])
	}
}

func TestBackstopFetch_FiltersOtherExchanges(t *testing.T) {
	withTestRESTs(t,
		func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`{"funding_rates":[
				{"exchange":"binance","symbol":"BTC","rate":0.0001},
				{"exchange":"lighter","symbol":"BTC","rate":0.00001}
			]}`))
		},
		func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`{"order_book_stats":[
				{"symbol":"BTC","last_trade_price":60000,"daily_quote_token_volume":1000000}
			]}`))
		},
	)
	ticks, _ := New().BackstopFetch(context.Background(), nil)
	if len(ticks) != 1 || ticks[0].Rate != 0.00001 {
		t.Errorf("lighter-only filter: should keep 0.00001 not 0.0001, got %+v", ticks)
	}
}

func TestBackstopFetch_SkipsStatsMissingFromRates(t *testing.T) {
	withTestRESTs(t,
		func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`{"funding_rates":[
				{"exchange":"lighter","symbol":"BTC","rate":0.00001}
			]}`))
		},
		func(w http.ResponseWriter, r *http.Request) {
			// ETH has stats but no funding-rate entry
			_, _ = w.Write([]byte(`{"order_book_stats":[
				{"symbol":"BTC","last_trade_price":60000,"daily_quote_token_volume":1000000},
				{"symbol":"ETH","last_trade_price":3000,"daily_quote_token_volume":500000}
			]}`))
		},
	)
	ticks, _ := New().BackstopFetch(context.Background(), nil)
	if len(ticks) != 1 || ticks[0].Symbol != "BTC" {
		t.Errorf("ETH without rate should be skipped: %+v", ticks)
	}
}

func TestBackstopFetch_RatesNon200Errors(t *testing.T) {
	withTestRESTs(t,
		func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusBadGateway) },
		func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`{"order_book_stats":[]}`))
		},
	)
	if _, err := New().BackstopFetch(context.Background(), nil); err == nil {
		t.Errorf("rates non-200 should error")
	}
}
