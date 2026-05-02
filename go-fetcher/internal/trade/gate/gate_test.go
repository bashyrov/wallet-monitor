package gate

import (
	"context"
	"encoding/hex"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

func TestSymbol(t *testing.T) {
	if got := toGateSymbol("btc"); got != "BTC_USDT" {
		t.Errorf("got %q", got)
	}
}

func TestSign_HMACSHA512(t *testing.T) {
	// HMAC-SHA512(secret, "GET\n/v4/test\n\n<sha512('')>\n123") expressed as hex
	const secret = "abc"
	got := gateSign(secret, "GET", "/v4/test", "", "", "123")
	if len(got) != 128 {
		t.Errorf("expected 128 hex chars (64 bytes SHA512), got %d", len(got))
	}
	if _, err := hex.DecodeString(got); err != nil {
		t.Errorf("expected valid hex: %v", err)
	}
}

func TestCoinsToContracts(t *testing.T) {
	if got := coinsToContracts(0.5, 0.0001); got != 5000 {
		t.Errorf("expected 5000, got %d", got)
	}
	if got := coinsToContracts(0.00005, 0.0001); got != 0 {
		t.Errorf("expected 0 (below quanto), got %d", got)
	}
}

func TestParseError_FriendlyMapped(t *testing.T) {
	body := []byte(`{"label":"INSUFFICIENT_BALANCE","message":"not enough"}`)
	te := parseError(http.StatusBadRequest, body)
	if !strings.Contains(te.Message, "Insufficient margin") {
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
		case strings.HasSuffix(r.URL.Path, "/futures/usdt/contracts"):
			io.WriteString(w, `[
				{"name":"BTC_USDT","quanto_multiplier":"0.0001","order_size_min":1,"order_size_max":1000000}
			]`)
		case strings.HasSuffix(r.URL.Path, "/futures/usdt/orders"):
			// Verify auth headers
			if r.Header.Get("KEY") != "k" {
				t.Errorf("missing KEY header")
			}
			if r.Header.Get("SIGN") == "" {
				t.Errorf("missing SIGN header")
			}
			if r.Header.Get("Timestamp") == "" {
				t.Errorf("missing Timestamp header")
			}
			body, _ := io.ReadAll(r.Body)
			s := string(body)
			if !strings.Contains(s, `"contract":"BTC_USDT"`) {
				t.Errorf("body missing contract: %s", s)
			}
			// 0.5 BTC at quanto 0.0001 = 5000 contracts; long → positive size.
			if !strings.Contains(s, `"size":5000`) {
				t.Errorf("expected size=5000 (long), got: %s", s)
			}
			io.WriteString(w, `{"id":12345,"fill_price":"50000","status":"finished"}`)
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
	if res.OrderID != "12345" {
		t.Errorf("got orderId %q", res.OrderID)
	}
	if res.Quantity != 0.5 {
		t.Errorf("got quantity %v, want 0.5", res.Quantity)
	}
}

func TestPlaceOrder_ShortNegativeSize(t *testing.T) {
	a, cleanup := newAdapterWithServer(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/futures/usdt/contracts"):
			io.WriteString(w, `[
				{"name":"BTC_USDT","quanto_multiplier":"0.0001","order_size_min":1,"order_size_max":1000000}
			]`)
		case strings.HasSuffix(r.URL.Path, "/futures/usdt/orders"):
			body, _ := io.ReadAll(r.Body)
			if !strings.Contains(string(body), `"size":-5000`) {
				t.Errorf("expected negative size for short, got: %s", body)
			}
			io.WriteString(w, `{"id":42}`)
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

func TestRegisteredViaInit(t *testing.T) {
	a := trade.Lookup("gate")
	if a == nil {
		t.Fatal("gate adapter not registered")
	}
	if a.Name() != "gate" {
		t.Errorf("got %q", a.Name())
	}
}
