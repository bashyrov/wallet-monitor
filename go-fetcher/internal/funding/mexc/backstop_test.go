package mexc

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

func TestBackstopFetch_DecodesContractTicker(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"data":[
			{"symbol":"BTC_USDT","lastPrice":60000,"fairPrice":60050,"indexPrice":60000,"fundingRate":0.0001,"nextSettleTime":1718000028000,"amount24":1000000}
		]}`))
	})
	ticks, err := New().BackstopFetch(context.Background(), nil)
	if err != nil {
		t.Fatalf("BackstopFetch: %v", err)
	}
	if len(ticks) != 1 || ticks[0].Symbol != "BTC" || ticks[0].MarkPrice != 60050 {
		t.Errorf("decoded: %+v", ticks)
	}
}

func TestBackstopFetch_MarkPriceFallsBackToLast(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"data":[
			{"symbol":"ETH_USDT","lastPrice":3000,"fundingRate":0.0001}
		]}`))
	})
	ticks, _ := New().BackstopFetch(context.Background(), nil)
	if len(ticks) != 1 || ticks[0].MarkPrice != 3000 {
		t.Errorf("fallback to lastPrice: %+v", ticks)
	}
}

func TestBackstopFetch_FiltersNonUSDT(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"data":[
			{"symbol":"BTC_USDT","fairPrice":60000,"fundingRate":0.0001},
			{"symbol":"BTC_USDC","fairPrice":60000,"fundingRate":0.0001}
		]}`))
	})
	ticks, _ := New().BackstopFetch(context.Background(), nil)
	if len(ticks) != 1 {
		t.Errorf("non-_USDT filtered: want 1 got %d", len(ticks))
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
