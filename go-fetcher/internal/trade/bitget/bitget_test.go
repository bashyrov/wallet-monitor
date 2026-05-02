package bitget

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
	if got := toBitgetSymbol("btc"); got != "BTCUSDT" {
		t.Errorf("got %q", got)
	}
}

func TestRequiresPassphrase(t *testing.T) {
	a := New()
	_, err := a.PlaceOrder(context.Background(),
		trade.Creds{APIKey: "k", APISecret: "s"}, // no passphrase
		trade.OpenRequest{
			Symbol: "BTC", Side: trade.SideBuy, Quantity: 0.1,
			Leverage: 5, MarginMode: trade.MarginIsolated,
		})
	if err == nil || !trade.IsUser(err) {
		t.Errorf("expected user error for missing passphrase, got %+v", err)
	}
}

func TestRoundToMultiplier(t *testing.T) {
	if got := roundToMultiplier(1.234, 0.01, 2); got != 1.23 {
		t.Errorf("got %v", got)
	}
	if got := roundToMultiplier(0.005, 0.01, 4); got != 0 {
		t.Errorf("got %v (expected floor below mult)", got)
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
		case strings.Contains(r.URL.Path, "/market/contracts"):
			io.WriteString(w, `{"code":"00000","data":[{
				"symbol":"BTCUSDT","sizeMultiplier":"0.001","volumePlace":"3","minTradeNum":"0.001"
			}]}`)
		case strings.HasSuffix(r.URL.Path, "/order/place-order"):
			for _, h := range []string{"ACCESS-KEY", "ACCESS-SIGN", "ACCESS-TIMESTAMP", "ACCESS-PASSPHRASE"} {
				if r.Header.Get(h) == "" {
					t.Errorf("missing header %s", h)
				}
			}
			body, _ := io.ReadAll(r.Body)
			s := string(body)
			if !strings.Contains(s, `"symbol":"BTCUSDT"`) {
				t.Errorf("body missing symbol: %s", s)
			}
			if !strings.Contains(s, `"side":"buy"`) {
				t.Errorf("expected side=buy, got %s", s)
			}
			if !strings.Contains(s, `"tradeSide":"open"`) {
				t.Errorf("expected tradeSide=open: %s", s)
			}
			io.WriteString(w, `{"code":"00000","data":{"orderId":"order-42","clientOid":"x"}}`)
		default:
			http.Error(w, "?", http.StatusNotFound)
		}
	})
	defer cleanup()

	res, err := a.PlaceOrder(context.Background(),
		trade.Creds{APIKey: "k", APISecret: "s", Passphrase: "p"},
		trade.OpenRequest{
			Symbol: "BTC", Side: trade.SideBuy, Quantity: 0.5,
			Leverage: 5, MarginMode: trade.MarginIsolated,
		})
	if err != nil {
		t.Fatalf("PlaceOrder failed: %v", err)
	}
	if res.OrderID != "order-42" {
		t.Errorf("got %q", res.OrderID)
	}
}

func TestRegisteredViaInit(t *testing.T) {
	a := trade.Lookup("bitget")
	if a == nil {
		t.Fatal("bitget adapter not registered")
	}
}
