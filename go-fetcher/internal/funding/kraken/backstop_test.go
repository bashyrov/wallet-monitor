package kraken

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

func TestBackstopFetch_DecodesTickers(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"tickers":[
			{"symbol":"PF_XBTUSD","markPrice":60000,"fundingRate":0.00005,"volumeQuote":1000000,"suspended":false}
		]}`))
	})
	ticks, err := New().BackstopFetch(context.Background(), nil)
	if err != nil {
		t.Fatalf("BackstopFetch: %v", err)
	}
	if len(ticks) != 1 || ticks[0].Symbol != "BTC" { // XBT alias
		t.Errorf("XBT should alias to BTC, got %+v", ticks)
	}
	if ticks[0].IntervalH != 1 {
		t.Errorf("Kraken interval: want 1h got %v", ticks[0].IntervalH)
	}
}

func TestBackstopFetch_SkipsSuspended(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"tickers":[
			{"symbol":"PF_XBTUSD","markPrice":60000,"fundingRate":0.00005,"suspended":true},
			{"symbol":"PF_ETHUSD","markPrice":3000,"fundingRate":0.00005,"suspended":false}
		]}`))
	})
	ticks, _ := New().BackstopFetch(context.Background(), nil)
	if len(ticks) != 1 || ticks[0].Symbol != "ETH" {
		t.Errorf("suspended should be skipped: %+v", ticks)
	}
}

func TestBackstopFetch_FiltersNonPFUSD(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"tickers":[
			{"symbol":"PF_XBTUSD","markPrice":60000,"fundingRate":0.00005},
			{"symbol":"FI_XBTUSD_240329","markPrice":60000,"fundingRate":0.00005}
		]}`))
	})
	ticks, _ := New().BackstopFetch(context.Background(), nil)
	if len(ticks) != 1 || ticks[0].Symbol != "BTC" {
		t.Errorf("non-PF prefix filtered: %+v", ticks)
	}
}

func TestBackstopFetch_EmptyResultErrors(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"tickers":[]}`))
	})
	if _, err := New().BackstopFetch(context.Background(), nil); err == nil {
		t.Errorf("empty results should error")
	}
}

func TestBackstopFetch_Non200Errors(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	})
	if _, err := New().BackstopFetch(context.Background(), nil); err == nil {
		t.Errorf("non-200 should error")
	}
}
