package paradex

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

func TestBackstopFetch_DecodesMarketsSummary(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"results":[
			{"symbol":"BTC-USD-PERP","mark_price":"60000","funding_rate":"0.00005","volume_24h":"1000000"}
		]}`))
	})
	ticks, err := New().BackstopFetch(context.Background(), nil)
	if err != nil {
		t.Fatalf("BackstopFetch: %v", err)
	}
	if len(ticks) != 1 || ticks[0].Symbol != "BTC" {
		t.Errorf("decoded: %+v", ticks)
	}
	if ticks[0].IntervalH != 8 {
		t.Errorf("interval: %v", ticks[0].IntervalH)
	}
}

func TestBackstopFetch_FiltersNonUSDPERP(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"results":[
			{"symbol":"BTC-USD-PERP","mark_price":"60000","funding_rate":"0.00005"},
			{"symbol":"BTC-USDC","mark_price":"60000","funding_rate":"0.00005"}
		]}`))
	})
	ticks, _ := New().BackstopFetch(context.Background(), nil)
	if len(ticks) != 1 {
		t.Errorf("non-USD-PERP filtered: want 1 got %d", len(ticks))
	}
}

func TestBackstopFetch_EmptyResultErrors(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"results":[]}`))
	})
	if _, err := New().BackstopFetch(context.Background(), nil); err == nil {
		t.Errorf("empty results should error")
	}
}

func TestBackstopFetch_NumericPriceAccepted(t *testing.T) {
	// Paradex docs say strings, but Tick.ParseFloat handles both.
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"results":[
			{"symbol":"ETH-USD-PERP","mark_price":3000,"funding_rate":0.00005,"volume_24h":500000}
		]}`))
	})
	ticks, err := New().BackstopFetch(context.Background(), nil)
	if err != nil {
		t.Fatalf("BackstopFetch: %v", err)
	}
	if len(ticks) != 1 || ticks[0].MarkPrice != 3000 {
		t.Errorf("numeric price: %+v", ticks)
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
