package bitget

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

func withTestFundRate(t *testing.T, handler http.HandlerFunc) {
	t.Helper()
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)
	orig := restFundRateURL
	restFundRateURL = srv.URL + "?symbol="
	t.Cleanup(func() { restFundRateURL = orig })
}

func TestBackstopFetch_DecodesBulkTickers(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"data":[
			{"symbol":"BTCUSDT","markPrice":"60050","indexPrice":"60000","fundingRate":"0.0001","quoteVolume":"1000000"}
		]}`))
	})
	// No symbols → skips current-fund-rate per-symbol calls
	ticks, err := New().BackstopFetch(context.Background(), nil)
	if err != nil {
		t.Fatalf("BackstopFetch: %v", err)
	}
	if len(ticks) != 1 || ticks[0].Symbol != "BTC" || ticks[0].MarkPrice != 60050 {
		t.Errorf("decoded: %+v", ticks)
	}
	if ticks[0].Volume24h != 1e6 {
		t.Errorf("volume: %v", ticks[0].Volume24h)
	}
}

func TestBackstopFetch_MarkPriceFallsBackToLastPr(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"data":[
			{"symbol":"ETHUSDT","lastPr":"3000","fundingRate":"0.0001"}
		]}`))
	})
	ticks, _ := New().BackstopFetch(context.Background(), nil)
	if len(ticks) != 1 || ticks[0].MarkPrice != 3000 {
		t.Errorf("fallback to lastPr: %+v", ticks)
	}
}

func TestBackstopFetch_FiltersNonUSDT(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"data":[
			{"symbol":"BTCUSDT","markPrice":"60000","fundingRate":"0.0001"},
			{"symbol":"BTCUSDC","markPrice":"60000","fundingRate":"0.0001"}
		]}`))
	})
	ticks, _ := New().BackstopFetch(context.Background(), nil)
	if len(ticks) != 1 {
		t.Errorf("non-USDT filtered: %d", len(ticks))
	}
}

func TestBackstopFetch_PerSymbolFundingRateMerged(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"data":[
			{"symbol":"BTCUSDT","markPrice":"60000","fundingRate":"0.0001"}
		]}`))
	})
	withTestFundRate(t, func(w http.ResponseWriter, r *http.Request) {
		// Per-symbol response with 4h interval (some Bitget pairs)
		_, _ = w.Write([]byte(`{"data":[{"fundingRateInterval":"4","nextUpdate":"1718000028000"}]}`))
	})
	ticks, err := New().BackstopFetch(context.Background(), []string{"BTC"})
	if err != nil {
		t.Fatalf("BackstopFetch: %v", err)
	}
	if len(ticks) != 1 {
		t.Fatalf("len: %d", len(ticks))
	}
	if ticks[0].IntervalH != 4 {
		t.Errorf("interval merged from per-symbol endpoint: want 4 got %v", ticks[0].IntervalH)
	}
	if ticks[0].NextFunding.UnixMilli() != 1718000028000 {
		t.Errorf("nextFunding merged: %v", ticks[0].NextFunding)
	}
}

func TestBackstopFetch_BulkNon200Errors(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
	})
	if _, err := New().BackstopFetch(context.Background(), nil); err == nil {
		t.Errorf("bulk non-200 should error")
	}
}
