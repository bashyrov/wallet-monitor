package aster

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

func TestBackstopFetch_DecodesPremiumIndex(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`[
			{"symbol":"BTCUSDT","markPrice":"60000","indexPrice":"60000","lastFundingRate":"0.0001","nextFundingTime":1718000028000}
		]`))
	})
	ticks, err := New().BackstopFetch(context.Background(), nil)
	if err != nil {
		t.Fatalf("BackstopFetch: %v", err)
	}
	if len(ticks) != 1 || ticks[0].Symbol != "BTC" || ticks[0].Rate != 0.0001 {
		t.Errorf("decoded: %+v", ticks)
	}
}

func TestBackstopFetch_FiltersNonUSDT(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`[
			{"symbol":"BTCUSDT","markPrice":"60000","lastFundingRate":"0.0001"},
			{"symbol":"BTCBUSD","markPrice":"60000","lastFundingRate":"0.0001"}
		]`))
	})
	ticks, _ := New().BackstopFetch(context.Background(), nil)
	if len(ticks) != 1 {
		t.Errorf("filter: want 1 got %d", len(ticks))
	}
}

func TestBackstopFetch_Non200Errors(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
	})
	if _, err := New().BackstopFetch(context.Background(), nil); err == nil {
		t.Errorf("non-200 should error")
	}
}
