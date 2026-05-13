package kucoin

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

func TestBackstopFetch_DecodesContracts(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"data":[
			{"symbol":"XBTUSDTM","markPrice":60000,"indexPrice":60000,"fundingFeeRate":0.0001,"nextFundingRateTime":3600000,"turnoverOf24h":1000000,"fundingRateInterval":8}
		]}`))
	})
	ticks, err := New().BackstopFetch(context.Background(), nil)
	if err != nil {
		t.Fatalf("BackstopFetch: %v", err)
	}
	if len(ticks) != 1 {
		t.Fatalf("len: %d", len(ticks))
	}
	if ticks[0].Symbol != "BTC" { // XBT alias
		t.Errorf("XBT should alias to BTC, got %s", ticks[0].Symbol)
	}
	if ticks[0].IntervalH != 8 {
		t.Errorf("interval: %v", ticks[0].IntervalH)
	}
}

func TestBackstopFetch_FiltersNonUSDTM(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"data":[
			{"symbol":"XBTUSDTM","markPrice":60000,"fundingFeeRate":0.0001},
			{"symbol":"XBTUSDM","markPrice":60000,"fundingFeeRate":0.0001}
		]}`))
	})
	ticks, _ := New().BackstopFetch(context.Background(), nil)
	if len(ticks) != 1 {
		t.Errorf("non-USDTM filtered: want 1 got %d", len(ticks))
	}
}

func TestBackstopFetch_FundingIntervalDefaultsTo8(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"data":[
			{"symbol":"ETHUSDTM","markPrice":3000,"fundingFeeRate":0.0001}
		]}`))
	})
	ticks, _ := New().BackstopFetch(context.Background(), nil)
	if ticks[0].IntervalH != 8 {
		t.Errorf("missing interval defaults to 8h, got %v", ticks[0].IntervalH)
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
