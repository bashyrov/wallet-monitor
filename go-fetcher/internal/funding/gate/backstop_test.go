package gate

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
)

func withTestRESTs(t *testing.T, contractsHandler, tickersHandler http.HandlerFunc) {
	t.Helper()
	csrv := httptest.NewServer(contractsHandler)
	tsrv := httptest.NewServer(tickersHandler)
	t.Cleanup(csrv.Close)
	t.Cleanup(tsrv.Close)
	origC, origT := contractsURL, tickersURL
	contractsURL = csrv.URL
	tickersURL = tsrv.URL
	t.Cleanup(func() {
		contractsURL = origC
		tickersURL = origT
	})
}

func TestBackstopFetch_DecodesContracts(t *testing.T) {
	withTestRESTs(t,
		func(w http.ResponseWriter, r *http.Request) {
			// /contracts is an array of objects
			_, _ = w.Write([]byte(`[
				{"name":"BTC_USDT","mark_price":"60000","index_price":"60000","funding_rate":"0.0001","funding_next_apply":1718000028,"funding_interval":28800}
			]`))
		},
		func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`[
				{"contract":"BTC_USDT","volume_24h_usd":"1000000000"}
			]`))
		},
	)
	ticks, err := New().BackstopFetch(context.Background(), nil)
	if err != nil {
		t.Fatalf("BackstopFetch: %v", err)
	}
	if len(ticks) != 1 || ticks[0].Symbol != "BTC" || ticks[0].Rate != 0.0001 {
		t.Errorf("decoded: %+v", ticks)
	}
	// 28800s ÷ 3600 = 8h
	if ticks[0].IntervalH != 8 {
		t.Errorf("interval: want 8h got %v", ticks[0].IntervalH)
	}
	if ticks[0].Volume24h != 1e9 {
		t.Errorf("volume joined: %v", ticks[0].Volume24h)
	}
}

func TestBackstopFetch_VolumeFallbackChain(t *testing.T) {
	withTestRESTs(t,
		func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`[
				{"name":"BTC_USDT","mark_price":"60000","funding_rate":"0.0001","funding_interval":28800},
				{"name":"ETH_USDT","mark_price":"3000","funding_rate":"0.0001","funding_interval":28800},
				{"name":"SOL_USDT","mark_price":"150","funding_rate":"0.0001","funding_interval":28800}
			]`))
		},
		func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`[
				{"contract":"BTC_USDT","volume_24h_usd":"1000"},
				{"contract":"ETH_USDT","volume_24h_quote":"500"},
				{"contract":"SOL_USDT","volume_24h_settle":"200"}
			]`))
		},
	)
	ticks, _ := New().BackstopFetch(context.Background(), nil)
	if len(ticks) != 3 {
		t.Fatalf("len: %d", len(ticks))
	}
	// Each tick gets volume from a different fallback field
	got := map[string]float64{}
	for _, tk := range ticks {
		got[tk.Symbol] = tk.Volume24h
	}
	if got["BTC"] != 1000 || got["ETH"] != 500 || got["SOL"] != 200 {
		t.Errorf("volume fallback chain: %v", got)
	}
}

func TestBackstopFetch_TickersFailureNonFatal(t *testing.T) {
	withTestRESTs(t,
		func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`[{"name":"BTC_USDT","mark_price":"60000","funding_rate":"0.0001","funding_interval":28800}]`))
		},
		func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusBadGateway)
		},
	)
	ticks, err := New().BackstopFetch(context.Background(), nil)
	if err != nil {
		t.Fatalf("tickers failure should be non-fatal, got %v", err)
	}
	if len(ticks) != 1 || ticks[0].Volume24h != 0 {
		t.Errorf("vol=0 expected when tickers fail: %+v", ticks)
	}
}

func TestBackstopFetch_ContractsNon200Errors(t *testing.T) {
	withTestRESTs(t,
		func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusBadGateway)
		},
		func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(`[]`))
		},
	)
	if _, err := New().BackstopFetch(context.Background(), nil); err == nil {
		t.Errorf("contracts non-200 should error")
	}
}
