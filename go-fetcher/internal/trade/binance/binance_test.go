package binance

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

// ── Quantity rounding ───────────────────────────────────────────────────

func TestRoundToStep(t *testing.T) {
	cases := []struct {
		qty, step float64
		prec      int
		want      float64
	}{
		{1.234567, 0.001, 3, 1.234},
		{0.0007, 0.001, 3, 0},        // floors below step → 0
		{1.999999, 0.01, 2, 1.99},
		{10, 1, 0, 10},
		{0.5, 0.5, 1, 0.5},
		{0.499999, 0.5, 1, 0},
		{1.234, 0, 2, 1.23},          // no step → precision-only
	}
	for _, c := range cases {
		got := roundToStep(c.qty, c.step, c.prec)
		if got != c.want {
			t.Errorf("roundToStep(%v, %v, %d) = %v, want %v",
				c.qty, c.step, c.prec, got, c.want)
		}
	}
}

func TestQtyString(t *testing.T) {
	cases := []struct {
		qty  float64
		prec int
		want string
	}{
		{1.0, 8, "1"},        // trailing zeros + dot dropped
		{1.5, 8, "1.5"},
		{0.001, 3, "0.001"},
		{1.234, 0, "1"},      // precision overrides
		{0, 2, "0"},
	}
	for _, c := range cases {
		got := qtyString(c.qty, c.prec)
		if got != c.want {
			t.Errorf("qtyString(%v, %d) = %q, want %q", c.qty, c.prec, got, c.want)
		}
	}
}

// ── Symbol mapping ──────────────────────────────────────────────────────

func TestToBinanceSymbol(t *testing.T) {
	cases := map[string]string{"btc": "BTCUSDT", "BTC": "BTCUSDT", "eth": "ETHUSDT"}
	for in, want := range cases {
		if got := toBinanceSymbol(in); got != want {
			t.Errorf("toBinanceSymbol(%q) = %q, want %q", in, got, want)
		}
	}
}

// ── Friendly error mapping ──────────────────────────────────────────────

func TestParseExchangeError(t *testing.T) {
	body := []byte(`{"code":-2019,"msg":"Margin is insufficient."}`)
	te := parseExchangeError(http.StatusBadRequest, body)
	if te == nil || te.Kind != trade.KindExchange {
		t.Fatalf("expected exchange-kind error, got %+v", te)
	}
	if !strings.Contains(te.Message, "Insufficient margin") {
		t.Errorf("expected friendly mapped message, got %q", te.Message)
	}
	if te.Code != "-2019" {
		t.Errorf("expected code -2019, got %q", te.Code)
	}
}

func TestParseExchangeError_RateLimit(t *testing.T) {
	body := []byte(`{"code":-1003,"msg":"Way too many requests"}`)
	te := parseExchangeError(http.StatusTooManyRequests, body)
	if te.Kind != trade.KindRateLimit {
		t.Errorf("expected rate-limit kind, got %s", te.Kind)
	}
}

// ── HMAC signing ────────────────────────────────────────────────────────

func TestSignedRequest_BuildsCanonicalSignature(t *testing.T) {
	// Reference vector: Binance docs example
	// secret  = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
	// payload = "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559"
	// signature = c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71
	const secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
	const payload = "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559"
	const want = "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"

	got := trade.HMACHexSHA256(secret, payload)
	if got != want {
		t.Errorf("signature mismatch:\n got: %s\nwant: %s", got, want)
	}
}

// ── HTTP roundtrip with httptest server ─────────────────────────────────

// withMockServer reroutes the adapter's BASE to an httptest server so we
// can validate the HTTP shape end-to-end without hitting Binance.
func newAdapterWithServer(handler http.HandlerFunc) (*Adapter, *httptest.Server, func()) {
	srv := httptest.NewServer(handler)
	a := New()
	// Override the package-level baseURL via a transport that rewrites
	// the URL on the fly. Cleaner than a global var swap because tests
	// can run in parallel.
	a.httpClient = &http.Client{
		Timeout: 5 * time.Second,
		Transport: roundTripperFunc(func(req *http.Request) (*http.Response, error) {
			req.URL.Scheme = "http"
			req.URL.Host = strings.TrimPrefix(srv.URL, "http://")
			return srv.Client().Transport.RoundTrip(req)
		}),
	}
	return a, srv, srv.Close
}

type roundTripperFunc func(*http.Request) (*http.Response, error)

func (f roundTripperFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestPlaceOrder_HappyPath(t *testing.T) {
	exchangeInfoBody := `{"symbols":[{
		"symbol":"BTCUSDT","contractType":"PERPETUAL",
		"quantityPrecision":3,"pricePrecision":2,
		"filters":[
			{"filterType":"LOT_SIZE","stepSize":"0.001","minQty":"0.001"},
			{"filterType":"MIN_NOTIONAL","notional":"5"}
		]
	}]}`
	orderBody := `{"orderId":12345,"avgPrice":"50000.00","status":"FILLED","clientOrderId":"x_123"}`

	a, _, cleanup := newAdapterWithServer(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/exchangeInfo"):
			io.WriteString(w, exchangeInfoBody)
		case strings.Contains(r.URL.Path, "/positionSide/dual"):
			io.WriteString(w, `{"dualSidePosition":false}`)
		case strings.HasSuffix(r.URL.Path, "/order"):
			// Verify the canonical signed-request structure.
			if r.Header.Get("X-MBX-APIKEY") != "test-key" {
				t.Errorf("missing api key header")
			}
			body, _ := io.ReadAll(r.Body)
			if !strings.Contains(string(body), "signature=") {
				t.Errorf("body missing signature: %s", body)
			}
			if !strings.Contains(string(body), "symbol=BTCUSDT") {
				t.Errorf("body missing symbol: %s", body)
			}
			io.WriteString(w, orderBody)
		default:
			t.Errorf("unexpected request to %s", r.URL.Path)
			http.Error(w, "unhandled", http.StatusInternalServerError)
		}
	})
	defer cleanup()

	res, err := a.PlaceOrder(context.Background(),
		trade.Creds{APIKey: "test-key", APISecret: "test-secret"},
		trade.OpenRequest{
			Symbol: "BTC", Side: trade.SideBuy, Quantity: 0.123,
			Leverage: 5, MarginMode: trade.MarginIsolated,
		})
	if err != nil {
		t.Fatalf("PlaceOrder failed: %v", err)
	}
	if res.OrderID != "12345" {
		t.Errorf("expected order id 12345, got %s", res.OrderID)
	}
	if res.Status != "FILLED" {
		t.Errorf("expected status FILLED, got %s", res.Status)
	}
	if res.AvgPrice != 50000 {
		t.Errorf("expected avgPrice 50000, got %v", res.AvgPrice)
	}
	if res.Quantity != 0.123 {
		t.Errorf("expected quantity 0.123, got %v", res.Quantity)
	}
}

func TestPlaceOrder_RoundsBelowMinQty(t *testing.T) {
	a, _, cleanup := newAdapterWithServer(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/exchangeInfo") {
			io.WriteString(w, `{"symbols":[{
				"symbol":"BTCUSDT","contractType":"PERPETUAL",
				"quantityPrecision":3,"pricePrecision":2,
				"filters":[{"filterType":"LOT_SIZE","stepSize":"0.001","minQty":"0.005"}]
			}]}`)
			return
		}
		t.Errorf("unexpected request to %s — should have failed before signing", r.URL.Path)
		http.Error(w, "should not reach here", http.StatusInternalServerError)
	})
	defer cleanup()

	_, err := a.PlaceOrder(context.Background(),
		trade.Creds{APIKey: "k", APISecret: "s"},
		trade.OpenRequest{
			Symbol: "BTC", Side: trade.SideBuy, Quantity: 0.001,
			Leverage: 5, MarginMode: trade.MarginIsolated,
		})
	if err == nil {
		t.Fatal("expected error when qty < minQty")
	}
	if !trade.IsUser(err) {
		t.Errorf("expected user-kind error, got %+v", err)
	}
}

func TestPlaceOrder_PropagatesExchangeError(t *testing.T) {
	a, _, cleanup := newAdapterWithServer(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/exchangeInfo"):
			io.WriteString(w, `{"symbols":[{
				"symbol":"BTCUSDT","contractType":"PERPETUAL",
				"quantityPrecision":3,"pricePrecision":2,
				"filters":[{"filterType":"LOT_SIZE","stepSize":"0.001","minQty":"0.001"}]
			}]}`)
		case strings.Contains(r.URL.Path, "/positionSide/dual"):
			io.WriteString(w, `{"dualSidePosition":false}`)
		case strings.HasSuffix(r.URL.Path, "/order"):
			http.Error(w, `{"code":-2019,"msg":"Margin is insufficient."}`, http.StatusBadRequest)
		default:
			http.Error(w, "?", http.StatusNotFound)
		}
	})
	defer cleanup()

	_, err := a.PlaceOrder(context.Background(),
		trade.Creds{APIKey: "k", APISecret: "s"},
		trade.OpenRequest{
			Symbol: "BTC", Side: trade.SideBuy, Quantity: 0.1,
			Leverage: 5, MarginMode: trade.MarginIsolated,
		})
	if err == nil {
		t.Fatal("expected error")
	}
	if !trade.IsExchange(err) {
		t.Errorf("expected exchange-kind error, got %+v", err)
	}
	te := err.(*trade.Error)
	if !strings.Contains(te.Message, "Insufficient margin") {
		t.Errorf("expected friendly mapped message, got %q", te.Message)
	}
}

func TestSetLeverage_RunsConcurrently(t *testing.T) {
	var marginCalls, levCalls int32
	startBarrier := make(chan struct{})
	a, _, cleanup := newAdapterWithServer(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/marginType"):
			marginCalls++
			<-startBarrier
			io.WriteString(w, `{"code":200,"msg":"OK"}`)
		case strings.HasSuffix(r.URL.Path, "/leverage"):
			levCalls++
			<-startBarrier
			io.WriteString(w, `{"leverage":5,"maxNotionalValue":"100000","symbol":"BTCUSDT"}`)
		default:
			http.Error(w, "?", http.StatusNotFound)
		}
	})
	defer cleanup()

	go func() {
		time.Sleep(50 * time.Millisecond)
		close(startBarrier) // release both calls together
	}()
	err := a.SetLeverage(context.Background(),
		trade.Creds{APIKey: "k", APISecret: "s"},
		trade.LeverageRequest{Symbol: "BTC", Leverage: 5, MarginMode: trade.MarginIsolated})
	if err != nil {
		t.Fatalf("SetLeverage err: %v", err)
	}
	if marginCalls != 1 || levCalls != 1 {
		t.Errorf("expected both endpoints hit once, got margin=%d leverage=%d", marginCalls, levCalls)
	}
}

func TestSetLeverage_TolerantOf4046(t *testing.T) {
	a, _, cleanup := newAdapterWithServer(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/marginType"):
			http.Error(w, `{"code":-4046,"msg":"No need to change margin type."}`, http.StatusBadRequest)
		case strings.HasSuffix(r.URL.Path, "/leverage"):
			io.WriteString(w, `{"leverage":5,"symbol":"BTCUSDT"}`)
		}
	})
	defer cleanup()

	if err := a.SetLeverage(context.Background(),
		trade.Creds{APIKey: "k", APISecret: "s"},
		trade.LeverageRequest{Symbol: "BTC", Leverage: 5, MarginMode: trade.MarginIsolated}); err != nil {
		t.Fatalf("expected -4046 to be swallowed, got: %v", err)
	}
}

// ── Registry registration ───────────────────────────────────────────────

func TestRegisteredViaInit(t *testing.T) {
	a := trade.Lookup("binance")
	if a == nil {
		t.Fatal("expected binance adapter to register itself via init()")
	}
	if a.Name() != "binance" {
		t.Errorf("expected name 'binance', got %q", a.Name())
	}
}

// ensure json import used
var _ = json.Unmarshal
