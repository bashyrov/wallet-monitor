package bybit

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

func TestBackstopFetch_DecodesTickersList(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"result":{"list":[
			{"symbol":"BTCUSDT","fundingRate":"0.0001","markPrice":"60000","indexPrice":"60000","nextFundingTime":"1718000028000","turnover24h":"1000000000"}
		]}}`))
	})
	ticks, err := New().BackstopFetch(context.Background(), nil)
	if err != nil {
		t.Fatalf("BackstopFetch: %v", err)
	}
	if len(ticks) != 1 || ticks[0].Symbol != "BTC" || ticks[0].Volume24h != 1e9 {
		t.Errorf("decoded: %+v", ticks)
	}
}

func TestBackstopFetch_FiltersNonUSDT(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"result":{"list":[
			{"symbol":"BTCUSDT","fundingRate":"0.0001","markPrice":"60000"},
			{"symbol":"BTCUSDC","fundingRate":"0.0001","markPrice":"60000"}
		]}}`))
	})
	ticks, _ := New().BackstopFetch(context.Background(), nil)
	if len(ticks) != 1 {
		t.Errorf("non-USDT filtered: want 1 got %d", len(ticks))
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
