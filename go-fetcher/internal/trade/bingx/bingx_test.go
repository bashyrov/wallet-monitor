package bingx

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

func TestSymbol(t *testing.T) {
	if got := toBingXSymbol("btc"); got != "BTC-USDT" {
		t.Errorf("got %q", got)
	}
}

func TestSignedQuery_AppendsSignature(t *testing.T) {
	q := signedQuery(map[string]string{"a": "1", "b": "2", "timestamp": "100"}, "secret")
	if !strings.Contains(q, "signature=") {
		t.Errorf("expected signature param: %q", q)
	}
	if !strings.HasPrefix(q, "a=1&b=2&timestamp=100") {
		t.Errorf("expected sorted params before signature: %q", q)
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
		switch {
		case strings.Contains(r.URL.Path, "/quote/contracts"):
			io.WriteString(w, `{"code":0,"data":[
				{"symbol":"BTC-USDT","status":1,"tradeMinQuantity":"0.001","quantityPrecision":"3","size":"0.001"}
			]}`)
		case strings.HasSuffix(r.URL.Path, "/trade/order"):
			if r.Header.Get("X-BX-APIKEY") != "k" {
				t.Errorf("missing X-BX-APIKEY")
			}
			q := r.URL.RawQuery
			if !strings.Contains(q, "signature=") {
				t.Errorf("expected signature in query: %s", q)
			}
			if !strings.Contains(q, "symbol=BTC-USDT") {
				t.Errorf("expected symbol: %s", q)
			}
			if !strings.Contains(q, "positionSide=LONG") {
				t.Errorf("expected positionSide=LONG: %s", q)
			}
			io.WriteString(w, `{"code":0,"data":{"order":{"orderId":42,"avgPrice":"50000","status":"FILLED"}}}`)
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
	if res.OrderID != "42" {
		t.Errorf("got %q", res.OrderID)
	}
}

func TestRegisteredViaInit(t *testing.T) {
	a := trade.Lookup("bingx")
	if a == nil {
		t.Fatal("bingx adapter not registered")
	}
}
