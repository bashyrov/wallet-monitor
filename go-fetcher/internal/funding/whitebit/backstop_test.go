package whitebit

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
)

func withTestREST(t *testing.T, handler http.HandlerFunc) {
	t.Helper()
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)
	orig := restURL
	restURL = srv.URL
	t.Cleanup(func() { restURL = orig })
}

func TestBackstopFetch_DecodesFutures(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"result":[
			{"ticker_id":"BTC_PERP","last_price":"60000","index_price":"60000","funding_rate":"0.0001","next_funding_rate_timestamp":"1718000028000","money_volume":"1000000","open_interest":"100"}
		]}`))
	})
	ticks, err := New().BackstopFetch(context.Background(), nil)
	if err != nil {
		t.Fatalf("BackstopFetch: %v", err)
	}
	if len(ticks) != 1 || ticks[0].Symbol != "BTC" {
		t.Errorf("decoded: %+v", ticks)
	}
	// OpenIntUSD = openInterest × last → 100 × 60000 = 6_000_000
	if ticks[0].OpenIntUSD != 6_000_000 {
		t.Errorf("OpenIntUSD: want 6e6 got %v", ticks[0].OpenIntUSD)
	}
}

func TestBackstopFetch_FiltersNonPERP(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"result":[
			{"ticker_id":"BTC_PERP","last_price":"60000","funding_rate":"0.0001"},
			{"ticker_id":"BTC_USDT","last_price":"60000","funding_rate":"0.0001"}
		]}`))
	})
	ticks, _ := New().BackstopFetch(context.Background(), nil)
	if len(ticks) != 1 {
		t.Errorf("non-_PERP filtered: want 1 got %d", len(ticks))
	}
}

func TestBackstopFetch_Non200Errors(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusGatewayTimeout)
	})
	if _, err := New().BackstopFetch(context.Background(), nil); err == nil {
		t.Errorf("non-200 should error")
	}
}
