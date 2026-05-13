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

func TestRoundQty_StepFloor(t *testing.T) {
	cases := []struct {
		qty, step float64
		prec      int
		want      float64
	}{
		{1.234, 0.001, 3, 1.234},
		{0.0007, 0.001, 3, 0},  // below step → 0
		{1.999, 0.01, 2, 1.99},
		{10, 1, 0, 10},
	}
	for _, c := range cases {
		if got := roundQty(c.qty, c.step, c.prec); got != c.want {
			t.Errorf("roundQty(%v,%v,%v): want %v got %v", c.qty, c.step, c.prec, c.want, got)
		}
	}
}

func TestQtyString_PrecisionTrim(t *testing.T) {
	cases := []struct {
		q    float64
		prec int
		want string
	}{
		{1.5, 3, "1.5"},
		{1.0, 3, "1"},
		{0.001, 3, "0.001"},
		{1.234567, 2, "1.23"},
		{0, 2, "0"},
	}
	for _, c := range cases {
		if got := qtyString(c.q, c.prec); got != c.want {
			t.Errorf("qtyString(%v,%v): want %q got %q", c.q, c.prec, c.want, got)
		}
	}
}

func TestFriendly_KnownCodeOverridesMessage(t *testing.T) {
	// 103009 → "Order qty below contract minimum."
	if got := friendly("103009", "raw bingx text"); got != "Order qty below contract minimum." {
		t.Errorf("known code should override raw msg, got %q", got)
	}
}

func TestFriendly_UnknownCodePassesThroughMessage(t *testing.T) {
	if got := friendly("999999", "raw error text"); got != "raw error text" {
		t.Errorf("unknown code should pass raw msg, got %q", got)
	}
}

func TestFriendly_UnknownCodeNoMessageReturnsDefault(t *testing.T) {
	got := friendly("99999", "")
	if got == "" {
		t.Errorf("default message should never be empty")
	}
}

func TestSignedQuery_DeterministicKeyOrder(t *testing.T) {
	q1 := signedQuery(map[string]string{"z": "1", "a": "2", "m": "3", "timestamp": "100"}, "k")
	q2 := signedQuery(map[string]string{"a": "2", "m": "3", "z": "1", "timestamp": "100"}, "k")
	if q1 != q2 {
		t.Errorf("query should be deterministic regardless of map iteration order:\n  %s\n  %s", q1, q2)
	}
}
