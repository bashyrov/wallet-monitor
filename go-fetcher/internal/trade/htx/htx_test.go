package htx

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
	if got := toContractCode("btc"); got != "BTC-USDT" {
		t.Errorf("got %q", got)
	}
}

func TestCanonicalQuery_ExcludesSignature(t *testing.T) {
	q := canonicalQuery(map[string]string{
		"AccessKeyId": "k", "Timestamp": "t", "Signature": "should-be-skipped",
	})
	if strings.Contains(q, "Signature") {
		t.Errorf("Signature MUST be excluded from sign-payload: %q", q)
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
		case strings.Contains(r.URL.Path, "/swap_contract_info"):
			io.WriteString(w, `{"status":"ok","data":[
				{"contract_code":"BTC-USDT","contract_size":"0.001"}
			]}`)
		case strings.HasSuffix(r.URL.Path, "/swap_cross_order"):
			q := r.URL.RawQuery
			if !strings.Contains(q, "AccessKeyId=k") {
				t.Errorf("missing AccessKeyId: %s", q)
			}
			if !strings.Contains(q, "Signature=") {
				t.Errorf("missing Signature: %s", q)
			}
			body, _ := io.ReadAll(r.Body)
			s := string(body)
			if !strings.Contains(s, `"contract_code":"BTC-USDT"`) {
				t.Errorf("body missing contract_code: %s", s)
			}
			// 0.5 / 0.001 = 500 contracts, direction=buy.
			if !strings.Contains(s, `"volume":500`) {
				t.Errorf("expected volume=500, got: %s", s)
			}
			if !strings.Contains(s, `"direction":"buy"`) {
				t.Errorf("expected direction=buy, got: %s", s)
			}
			io.WriteString(w, `{"status":"ok","data":{"order_id_str":"htx-42"}}`)
		default:
			http.Error(w, "?", http.StatusNotFound)
		}
	})
	defer cleanup()

	res, err := a.PlaceOrder(context.Background(),
		trade.Creds{APIKey: "k", APISecret: "s"},
		trade.OpenRequest{
			Symbol: "BTC", Side: trade.SideBuy, Quantity: 0.5,
			Leverage: 5, MarginMode: trade.MarginCross,
		})
	if err != nil {
		t.Fatalf("PlaceOrder failed: %v", err)
	}
	if res.OrderID != "htx-42" {
		t.Errorf("got %q", res.OrderID)
	}
}

func TestRegisteredViaInit(t *testing.T) {
	a := trade.Lookup("htx")
	if a == nil {
		t.Fatal("htx adapter not registered")
	}
}
