package binance

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
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`[
			{"symbol":"BTCUSDT","markPrice":"60000.5","indexPrice":"60000.0","lastFundingRate":"0.0001","nextFundingTime":1718000028000},
			{"symbol":"ETHUSDT","markPrice":"3000","indexPrice":"2999","lastFundingRate":"-0.0002","nextFundingTime":1718000028000}
		]`))
	})

	a := New()
	ticks, err := a.BackstopFetch(context.Background(), nil)
	if err != nil {
		t.Fatalf("BackstopFetch: %v", err)
	}
	if len(ticks) != 2 {
		t.Fatalf("len: want 2 got %d", len(ticks))
	}
	if ticks[0].Symbol != "BTC" || ticks[0].MarkPrice != 60000.5 || ticks[0].Rate != 0.0001 {
		t.Errorf("BTC: %+v", ticks[0])
	}
	if ticks[1].Symbol != "ETH" || ticks[1].Rate != -0.0002 {
		t.Errorf("ETH (negative): %+v", ticks[1])
	}
	if ticks[0].IntervalH != 8 {
		t.Errorf("interval: want 8 got %v", ticks[0].IntervalH)
	}
}

func TestBackstopFetch_FiltersNonUSDTSymbols(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`[
			{"symbol":"BTCUSDT","markPrice":"60000","lastFundingRate":"0.0001"},
			{"symbol":"BTCBUSD","markPrice":"60000","lastFundingRate":"0.0001"}
		]`))
	})

	a := New()
	ticks, _ := a.BackstopFetch(context.Background(), nil)
	if len(ticks) != 1 {
		t.Fatalf("filter: want 1 got %d", len(ticks))
	}
	if ticks[0].Symbol != "BTC" {
		t.Errorf("kept wrong symbol: %v", ticks[0].Symbol)
	}
}

func TestBackstopFetch_Non200Errors(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	})
	_, err := New().BackstopFetch(context.Background(), nil)
	if err == nil {
		t.Errorf("non-200 should error")
	}
}

func TestBackstopFetch_BackstopIntervalIs2s(t *testing.T) {
	if a := New(); a.BackstopInterval().Seconds() != 2 {
		t.Errorf("BackstopInterval: want 2s got %v", a.BackstopInterval())
	}
}
