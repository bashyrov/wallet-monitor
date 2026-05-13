package hyperliquid

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

func TestBackstopFetch_DecodesMetaAndAssetCtxs(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		// HL POST endpoint — array-of-2 response.
		_, _ = w.Write([]byte(`[
			{"universe":[{"name":"BTC","isDelisted":false},{"name":"ETH","isDelisted":false}]},
			[
				{"funding":"0.00005","markPx":"60000","oraclePx":"60000","dayNtlVlm":"1000000","openInterest":"100"},
				{"funding":"0.00010","markPx":"3000","oraclePx":"3000","dayNtlVlm":"500000","openInterest":"50"}
			]
		]`))
	})
	ticks, err := New().BackstopFetch(context.Background(), nil)
	if err != nil {
		t.Fatalf("BackstopFetch: %v", err)
	}
	if len(ticks) != 2 {
		t.Fatalf("len: %d", len(ticks))
	}
	if ticks[0].Symbol != "BTC" || ticks[0].Rate != 0.00005 {
		t.Errorf("BTC: %+v", ticks[0])
	}
	if ticks[0].IntervalH != 1 {
		t.Errorf("HL interval: want 1h got %v", ticks[0].IntervalH)
	}
	// OpenIntUSD = openInterest × mark → 100 × 60000 = 6_000_000
	if ticks[0].OpenIntUSD != 6_000_000 {
		t.Errorf("OpenIntUSD: %v", ticks[0].OpenIntUSD)
	}
}

func TestBackstopFetch_SkipsDelisted(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`[
			{"universe":[{"name":"CYBER","isDelisted":true},{"name":"BTC","isDelisted":false}]},
			[
				{"funding":"0","markPx":"5","oraclePx":"5","dayNtlVlm":"0","openInterest":"0"},
				{"funding":"0.00005","markPx":"60000","oraclePx":"60000","dayNtlVlm":"1000000","openInterest":"100"}
			]
		]`))
	})
	ticks, _ := New().BackstopFetch(context.Background(), nil)
	if len(ticks) != 1 || ticks[0].Symbol != "BTC" {
		t.Errorf("delisted (CYBER) should be skipped: %+v", ticks)
	}
}

func TestBackstopFetch_MetaCtxsLenMismatchErrors(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`[
			{"universe":[{"name":"BTC","isDelisted":false}]},
			[{"funding":"0.00005","markPx":"60000"},{"funding":"0.0001","markPx":"3000"}]
		]`))
	})
	if _, err := New().BackstopFetch(context.Background(), nil); err == nil {
		t.Errorf("meta/ctxs length mismatch should error")
	}
}

func TestBackstopFetch_MalformedResponseErrors(t *testing.T) {
	withTestREST(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"not":"an array"}`))
	})
	if _, err := New().BackstopFetch(context.Background(), nil); err == nil {
		t.Errorf("malformed shape should error")
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
