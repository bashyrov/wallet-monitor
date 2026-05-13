package extended

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

func TestBackstopFetch_DecodesActiveMarkets(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"data":[
			{"name":"BTC-USD","status":"ACTIVE","active":true,"marketStats":{
				"markPrice":"60000","lastPrice":"59999","fundingRate":"0.00001","dailyVolume":"1000000","nextFundingRate":1718000028000
			}}
		]}`))
	})
	ticks, err := New().BackstopFetch(context.Background(), nil)
	if err != nil {
		t.Fatalf("BackstopFetch: %v", err)
	}
	if len(ticks) != 1 || ticks[0].Symbol != "BTC" {
		t.Errorf("decoded: %+v", ticks)
	}
	if ticks[0].IntervalH != 1 {
		t.Errorf("interval: %v", ticks[0].IntervalH)
	}
}

func TestBackstopFetch_SkipsInactive(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"data":[
			{"name":"DEAD-USD","status":"DISABLED","active":false,"marketStats":{"markPrice":"1","fundingRate":"0.0001"}},
			{"name":"BTC-USD","status":"ACTIVE","active":true,"marketStats":{"markPrice":"60000","fundingRate":"0.00001"}}
		]}`))
	})
	ticks, _ := New().BackstopFetch(context.Background(), nil)
	if len(ticks) != 1 || ticks[0].Symbol != "BTC" {
		t.Errorf("inactive should be skipped: %+v", ticks)
	}
}

func TestBackstopFetch_MarkPriceFallsBackToLast(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"data":[
			{"name":"ETH-USD","status":"ACTIVE","active":true,"marketStats":{"lastPrice":"3000","fundingRate":"0.00001"}}
		]}`))
	})
	ticks, _ := New().BackstopFetch(context.Background(), nil)
	if len(ticks) != 1 || ticks[0].MarkPrice != 3000 {
		t.Errorf("fallback to lastPrice: %+v", ticks)
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
