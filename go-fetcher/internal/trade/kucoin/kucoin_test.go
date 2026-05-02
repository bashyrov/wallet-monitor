package kucoin

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
	cases := map[string]string{
		"btc": "XBTUSDTM", "BTC": "XBTUSDTM", "eth": "ETHUSDTM",
	}
	for in, want := range cases {
		if got := toKucoinSymbol(in); got != want {
			t.Errorf("toKucoinSymbol(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestRequiresPassphrase(t *testing.T) {
	a := New()
	_, err := a.PlaceOrder(context.Background(),
		trade.Creds{APIKey: "k", APISecret: "s"},
		trade.OpenRequest{
			Symbol: "BTC", Side: trade.SideBuy, Quantity: 0.5,
			Leverage: 5, MarginMode: trade.MarginIsolated,
		})
	if err == nil || !trade.IsUser(err) {
		t.Errorf("expected user error for missing passphrase, got %+v", err)
	}
}

func TestCoinsToContracts(t *testing.T) {
	if got := coinsToContracts(0.5, 0.001, 1); got != 500 {
		t.Errorf("got %d", got)
	}
	if got := coinsToContracts(0.0005, 0.001, 1); got != 0 {
		t.Errorf("got %d", got)
	}
}

func TestUUIDIsRFC4122v4(t *testing.T) {
	u := uuid()
	if len(u) != 36 {
		t.Fatalf("expected 36 chars, got %d (%q)", len(u), u)
	}
	if u[14] != '4' {
		t.Errorf("v4 indicator missing in %q", u)
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
		case strings.Contains(r.URL.Path, "/contracts/"):
			io.WriteString(w, `{"code":"200000","data":{
				"multiplier":"0.001","lotSize":"1","maxLeverage":"100"
			}}`)
		case strings.HasSuffix(r.URL.Path, "/orders"):
			for _, h := range []string{"KC-API-KEY", "KC-API-SIGN", "KC-API-TIMESTAMP", "KC-API-PASSPHRASE", "KC-API-KEY-VERSION"} {
				if r.Header.Get(h) == "" {
					t.Errorf("missing header %s", h)
				}
			}
			body, _ := io.ReadAll(r.Body)
			s := string(body)
			if !strings.Contains(s, `"symbol":"XBTUSDTM"`) {
				t.Errorf("body missing XBT symbol: %s", s)
			}
			if !strings.Contains(s, `"size":500`) {
				t.Errorf("expected size=500 (0.5 / 0.001), got: %s", s)
			}
			if !strings.Contains(s, `"marginMode":"ISOLATED"`) {
				t.Errorf("expected marginMode=ISOLATED, got: %s", s)
			}
			io.WriteString(w, `{"code":"200000","data":{"orderId":"k-42","clientOid":"x"}}`)
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
	if res.OrderID != "k-42" {
		t.Errorf("got %q", res.OrderID)
	}
}

func TestRegisteredViaInit(t *testing.T) {
	a := trade.Lookup("kucoin")
	if a == nil {
		t.Fatal("kucoin adapter not registered")
	}
}
