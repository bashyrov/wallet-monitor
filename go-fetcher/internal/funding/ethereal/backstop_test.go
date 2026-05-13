package ethereal

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// Ethereal uses baseURL with multiple paths (/v1/product + /v1/product/market-price).
// Single httptest server routes by path.
func withTestEndpoint(t *testing.T, handler http.HandlerFunc) {
	t.Helper()
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)
	orig := baseURL
	baseURL = srv.URL
	t.Cleanup(func() { baseURL = orig })
}

func TestBackstopFetch_DecodesProductPlusPrice(t *testing.T) {
	withTestEndpoint(t, func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasPrefix(r.URL.Path, "/v1/product/market-price"):
			_, _ = w.Write([]byte(`{"data":[
				{"productId":"prod-btc","oraclePrice":"60000"}
			]}`))
		case strings.HasPrefix(r.URL.Path, "/v1/product"):
			_, _ = w.Write([]byte(`{"data":[
				{"id":"prod-btc","baseTokenName":"BTC","fundingRate1h":"0.00001","status":"ACTIVE","openInterest":"100"}
			]}`))
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	})
	ticks, err := New().BackstopFetch(context.Background(), nil)
	if err != nil {
		t.Fatalf("BackstopFetch: %v", err)
	}
	if len(ticks) != 1 || ticks[0].Symbol != "BTC" {
		t.Errorf("decoded: %+v", ticks)
	}
	// OpenIntUSD = 100 × 60000 = 6_000_000
	if ticks[0].OpenIntUSD != 6_000_000 {
		t.Errorf("OpenIntUSD: %v", ticks[0].OpenIntUSD)
	}
	if ticks[0].IntervalH != 1 {
		t.Errorf("Ethereal interval: want 1h got %v", ticks[0].IntervalH)
	}
}

func TestBackstopFetch_SkipsInactiveProducts(t *testing.T) {
	withTestEndpoint(t, func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasPrefix(r.URL.Path, "/v1/product/market-price"):
			_, _ = w.Write([]byte(`{"data":[{"productId":"prod-btc","oraclePrice":"60000"}]}`))
		case strings.HasPrefix(r.URL.Path, "/v1/product"):
			_, _ = w.Write([]byte(`{"data":[
				{"id":"prod-dead","baseTokenName":"DEAD","fundingRate1h":"0.0001","status":"DISABLED"},
				{"id":"prod-btc","baseTokenName":"BTC","fundingRate1h":"0.00001","status":"ACTIVE"}
			]}`))
		}
	})
	ticks, _ := New().BackstopFetch(context.Background(), nil)
	if len(ticks) != 1 || ticks[0].Symbol != "BTC" {
		t.Errorf("inactive product skipped: %+v", ticks)
	}
}

func TestBackstopFetch_NoProductsErrors(t *testing.T) {
	withTestEndpoint(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"data":[]}`))
	})
	if _, err := New().BackstopFetch(context.Background(), nil); err == nil {
		t.Errorf("no products should error")
	}
}

func TestBackstopFetch_NoActiveProductsErrors(t *testing.T) {
	withTestEndpoint(t, func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasPrefix(r.URL.Path, "/v1/product"):
			_, _ = w.Write([]byte(`{"data":[
				{"id":"x","baseTokenName":"BTC","fundingRate1h":"0","status":"DISABLED"}
			]}`))
		}
	})
	if _, err := New().BackstopFetch(context.Background(), nil); err == nil {
		t.Errorf("no active products should error")
	}
}

func TestBackstopFetch_ProductNon200Errors(t *testing.T) {
	withTestEndpoint(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
	})
	if _, err := New().BackstopFetch(context.Background(), nil); err == nil {
		t.Errorf("product endpoint non-200 should error")
	}
}
