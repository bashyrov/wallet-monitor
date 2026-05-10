package bybit

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
	cases := map[string]string{"btc": "BTCUSDT", "eth": "ETHUSDT", "BTC": "BTCUSDT"}
	for in, want := range cases {
		if got := toBybit(in); got != want {
			t.Errorf("toBybit(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestRoundToStep(t *testing.T) {
	if got := roundToStep(1.23456, 0.01, 0); got != 1.23 {
		t.Errorf("expected 1.23, got %v", got)
	}
	if got := roundToStep(0.005, 0.01, 0.01); got != 0 {
		t.Errorf("expected 0 (below minQty), got %v", got)
	}
	if got := roundToStep(0.5, 0.5, 0.5); got != 0.5 {
		t.Errorf("expected 0.5, got %v", got)
	}
}

func TestParseError_RateLimit(t *testing.T) {
	body := []byte(`{"retCode":10006,"retMsg":"too many"}`)
	te := parseError(http.StatusOK, body)
	if te.Kind != trade.KindRateLimit {
		t.Errorf("expected rate-limit kind, got %s", te.Kind)
	}
}

func TestParseError_FriendlyMapped(t *testing.T) {
	body := []byte(`{"retCode":110017,"retMsg":"qty too small"}`)
	te := parseError(http.StatusOK, body)
	if !strings.Contains(te.Message, "below minimum") {
		t.Errorf("expected friendly mapped, got %q", te.Message)
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
		case strings.Contains(r.URL.Path, "/v5/market/instruments-info"):
			io.WriteString(w, `{"retCode":0,"result":{"list":[{
				"symbol":"BTCUSDT","status":"Trading",
				"lotSizeFilter":{"qtyStep":"0.001","minOrderQty":"0.001"},
				"priceFilter":{"tickSize":"0.5"}
			}]}}`)
		case strings.HasSuffix(r.URL.Path, "/v5/order/create"):
			// Verify signed headers are present
			if r.Header.Get("X-BAPI-API-KEY") != "key" {
				t.Errorf("missing X-BAPI-API-KEY")
			}
			if r.Header.Get("X-BAPI-SIGN") == "" {
				t.Errorf("missing X-BAPI-SIGN")
			}
			if r.Header.Get("X-BAPI-TIMESTAMP") == "" {
				t.Errorf("missing X-BAPI-TIMESTAMP")
			}
			body, _ := io.ReadAll(r.Body)
			if !strings.Contains(string(body), `"symbol":"BTCUSDT"`) {
				t.Errorf("body missing symbol: %s", body)
			}
			io.WriteString(w, `{"retCode":0,"result":{"orderId":"abc123"}}`)
		case strings.HasSuffix(r.URL.Path, "/v5/order/history"):
			// avg_price follow-up query — return filled order
			io.WriteString(w, `{"retCode":0,"result":{"list":[{"avgPrice":"43000.5"}]}}`)
		default:
			t.Errorf("unexpected path %s", r.URL.Path)
			http.Error(w, "?", http.StatusNotFound)
		}
	})
	defer cleanup()

	res, err := a.PlaceOrder(context.Background(),
		trade.Creds{APIKey: "key", APISecret: "secret"},
		trade.OpenRequest{
			Symbol: "BTC", Side: trade.SideBuy, Quantity: 0.123,
			Leverage: 5, MarginMode: trade.MarginIsolated,
		})
	if err != nil {
		t.Fatalf("PlaceOrder failed: %v", err)
	}
	if res.OrderID != "abc123" {
		t.Errorf("expected orderId abc123, got %q", res.OrderID)
	}
}

func TestPlaceOrder_BelowMinQty(t *testing.T) {
	a, cleanup := newAdapterWithServer(func(w http.ResponseWriter, r *http.Request) {
		if !strings.Contains(r.URL.Path, "/instruments-info") {
			t.Errorf("should not have hit %s", r.URL.Path)
		}
		io.WriteString(w, `{"retCode":0,"result":{"list":[{
			"symbol":"BTCUSDT","status":"Trading",
			"lotSizeFilter":{"qtyStep":"0.001","minOrderQty":"0.005"},
			"priceFilter":{"tickSize":"0.5"}
		}]}}`)
	})
	defer cleanup()

	_, err := a.PlaceOrder(context.Background(),
		trade.Creds{APIKey: "k", APISecret: "s"},
		trade.OpenRequest{
			Symbol: "BTC", Side: trade.SideBuy, Quantity: 0.001,
			Leverage: 5, MarginMode: trade.MarginIsolated,
		})
	if err == nil {
		t.Fatal("expected error when qty < minOrderQty")
	}
	if !trade.IsUser(err) {
		t.Errorf("expected user-kind error, got %+v", err)
	}
}

func TestRegisteredViaInit(t *testing.T) {
	a := trade.Lookup("bybit")
	if a == nil {
		t.Fatal("bybit adapter not registered")
	}
	if a.Name() != "bybit" {
		t.Errorf("expected 'bybit', got %q", a.Name())
	}
}
