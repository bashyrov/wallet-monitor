package mexc

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

func TestSymbolMapping(t *testing.T) {
	if got := toMexcSymbol("btc"); got != "BTC_USDT" {
		t.Errorf("got %q", got)
	}
}

func TestCoinsToContracts(t *testing.T) {
	// 0.5 BTC at contractSize=0.0001 = 5000 contracts; volUnit 1 → no extra rounding
	if got := coinsToContracts(0.5, 0.0001, 1); got != 5000 {
		t.Errorf("got %d", got)
	}
	// volUnit 10 → round to 10s; 5005 → 5000
	if got := coinsToContracts(0.50005, 0.0001, 10); got != 5000 {
		t.Errorf("expected 5000 (rounded to volUnit), got %d", got)
	}
	// Below contractSize → 0
	if got := coinsToContracts(0.00005, 0.0001, 1); got != 0 {
		t.Errorf("expected 0, got %d", got)
	}
}

func newAdapterWithServer(handler http.HandlerFunc) (*Adapter, func()) {
	srv := httptest.NewServer(handler)
	a := New()
	a.httpClient = &http.Client{
		Timeout: 5 * time.Second,
		Transport: roundTripperFunc(func(req *http.Request) (*http.Response, error) {
			req.URL.Scheme = "http"
			req.URL.Host = strings.TrimPrefix(srv.URL, "http://")
			return srv.Client().Transport.RoundTrip(req)
		}),
	}
	return a, srv.Close
}

type roundTripperFunc func(*http.Request) (*http.Response, error)

func (f roundTripperFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestPlaceOrder_HappyPath(t *testing.T) {
	a, cleanup := newAdapterWithServer(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case strings.Contains(r.URL.Path, "/contract/detail"):
			io.WriteString(w, `{"code":0,"data":{
				"minVol":"1","maxVol":"1000000","contractSize":"0.0001","volUnit":"1","maxLeverage":"100"
			}}`)
		case strings.HasSuffix(r.URL.Path, "/order/submit"):
			// Verify auth headers
			for _, h := range []string{"ApiKey", "Request-Time", "Signature"} {
				if r.Header.Get(h) == "" {
					t.Errorf("missing header %s", h)
				}
			}
			body, _ := io.ReadAll(r.Body)
			s := string(body)
			if !strings.Contains(s, `"symbol":"BTC_USDT"`) {
				t.Errorf("body missing symbol: %s", s)
			}
			if !strings.Contains(s, `"side":1`) {
				t.Errorf("expected side=1 (open_long), got: %s", s)
			}
			if !strings.Contains(s, `"vol":5000`) {
				t.Errorf("expected vol=5000, got: %s", s)
			}
			io.WriteString(w, `{"code":0,"data":"abc-order-id"}`)
		default:
			http.Error(w, "?", http.StatusNotFound)
		}
	})
	defer cleanup()

	res, err := a.PlaceOrder(context.Background(),
		trade.Creds{APIKey: "k", APISecret: "s"},
		trade.OpenRequest{
			Symbol: "BTC", Side: trade.SideBuy, Quantity: 0.5,
			Leverage: 5, MarginMode: trade.MarginIsolated,
		})
	if err != nil {
		t.Fatalf("PlaceOrder failed: %v", err)
	}
	if res.OrderID != "abc-order-id" {
		t.Errorf("got orderId %q", res.OrderID)
	}
	if res.Quantity != 0.5 {
		t.Errorf("got quantity %v, want 0.5", res.Quantity)
	}
}

func TestPlaceOrder_ShortSideEncoding(t *testing.T) {
	a, cleanup := newAdapterWithServer(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case strings.Contains(r.URL.Path, "/contract/detail"):
			io.WriteString(w, `{"code":0,"data":{"minVol":"1","contractSize":"0.0001","volUnit":"1"}}`)
		case strings.HasSuffix(r.URL.Path, "/order/submit"):
			body, _ := io.ReadAll(r.Body)
			if !strings.Contains(string(body), `"side":3`) {
				t.Errorf("expected side=3 (open_short) for sell, got: %s", body)
			}
			io.WriteString(w, `{"code":0,"data":"x"}`)
		}
	})
	defer cleanup()

	_, err := a.PlaceOrder(context.Background(),
		trade.Creds{APIKey: "k", APISecret: "s"},
		trade.OpenRequest{
			Symbol: "BTC", Side: trade.SideSell, Quantity: 0.5,
			Leverage: 5, MarginMode: trade.MarginIsolated,
		})
	if err != nil {
		t.Fatalf("PlaceOrder failed: %v", err)
	}
}

func TestEdgeBlockReturnsCleanError(t *testing.T) {
	a, cleanup := newAdapterWithServer(func(w http.ResponseWriter, r *http.Request) {
		// Akamai-style block: text/html "Access Denied".
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.WriteHeader(http.StatusForbidden)
		io.WriteString(w, "<html><body>Access Denied</body></html>")
	})
	defer cleanup()

	_, err := a.GetBalance(context.Background(),
		trade.Creds{APIKey: "k", APISecret: "s"})
	if err == nil {
		t.Fatal("expected error on edge-block")
	}
	te := err.(*trade.Error)
	if !strings.Contains(te.Message, "edge") {
		t.Errorf("expected friendly edge-block message, got %q", te.Message)
	}
}

func TestRegisteredViaInit(t *testing.T) {
	a := trade.Lookup("mexc")
	if a == nil {
		t.Fatal("mexc adapter not registered")
	}
}
