package okx

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
)

func withTestTickers(t *testing.T, handler http.HandlerFunc) {
	t.Helper()
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)
	orig := restTickersURL
	restTickersURL = srv.URL
	t.Cleanup(func() { restTickersURL = orig })
}

func withTestFundingRate(t *testing.T, handler http.HandlerFunc) {
	t.Helper()
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)
	orig := restFundingRateURL
	restFundingRateURL = srv.URL + "?instId="
	t.Cleanup(func() { restFundingRateURL = orig })
}

func TestBackstopFetch_BulkTickersHappy(t *testing.T) {
	withTestTickers(t, func(w http.ResponseWriter, r *http.Request) {
		// volCcy24h is in BASE units → adapter converts to USD via mark.
		_, _ = w.Write([]byte(`{"data":[
			{"instId":"BTC-USDT-SWAP","last":"60000","idxPx":"60000","volCcy24h":"10000"}
		]}`))
	})
	// No symbols → no per-symbol funding-rate fetch
	ticks, err := New().BackstopFetch(context.Background(), nil)
	if err != nil {
		t.Fatalf("BackstopFetch: %v", err)
	}
	if len(ticks) != 1 || ticks[0].Symbol != "BTC" {
		t.Errorf("decoded: %+v", ticks)
	}
	// 10000 BTC × 60000 USD = 600M USD
	if ticks[0].Volume24h != 6e8 {
		t.Errorf("volume BASE→USD conversion: want 6e8 got %v", ticks[0].Volume24h)
	}
}

func TestBackstopFetch_FiltersNonSWAPSuffix(t *testing.T) {
	withTestTickers(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"data":[
			{"instId":"BTC-USDT-SWAP","last":"60000","idxPx":"60000","volCcy24h":"10"},
			{"instId":"BTC-USDT","last":"60000","idxPx":"60000","volCcy24h":"10"}
		]}`))
	})
	ticks, _ := New().BackstopFetch(context.Background(), nil)
	if len(ticks) != 1 {
		t.Errorf("non-SWAP filtered: want 1 got %d", len(ticks))
	}
}

func TestBackstopFetch_PerSymbolFundingRateMerged(t *testing.T) {
	withTestTickers(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"data":[
			{"instId":"BTC-USDT-SWAP","last":"60000","idxPx":"60000","volCcy24h":"10"}
		]}`))
	})
	withTestFundingRate(t, func(w http.ResponseWriter, r *http.Request) {
		// 4h interval (nextFundingTime - fundingTime = 4×3600×1000 ms)
		_, _ = w.Write([]byte(`{"data":[{"fundingRate":"0.0001","nextFundingTime":"1718014400000","fundingTime":"1718000000000"}]}`))
	})
	ticks, err := New().BackstopFetch(context.Background(), []string{"BTC"})
	if err != nil {
		t.Fatalf("BackstopFetch: %v", err)
	}
	if len(ticks) != 1 {
		t.Fatalf("len: %d", len(ticks))
	}
	if ticks[0].Rate != 0.0001 {
		t.Errorf("rate from per-symbol endpoint: %v", ticks[0].Rate)
	}
	if ticks[0].IntervalH != 4 {
		t.Errorf("interval derived from times: want 4 got %v", ticks[0].IntervalH)
	}
}

func TestBackstopFetch_TickersNon200Errors(t *testing.T) {
	withTestTickers(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
	})
	if _, err := New().BackstopFetch(context.Background(), nil); err == nil {
		t.Errorf("tickers non-200 should error")
	}
}
