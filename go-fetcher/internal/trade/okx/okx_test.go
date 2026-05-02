package okx

import (
	"context"
	"encoding/base64"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

func TestSymbolMapping(t *testing.T) {
	if got := toOKXSymbol("btc"); got != "BTC-USDT-SWAP" {
		t.Errorf("got %q", got)
	}
	if got := toOKXSymbol("ETH"); got != "ETH-USDT-SWAP" {
		t.Errorf("got %q", got)
	}
}

func TestSign_Base64SHA256(t *testing.T) {
	// HMAC-SHA256("secret", "ts+method+path+body") then base64.
	ts := "2026-05-02T12:00:00.000Z"
	sig := trade.HMACBase64SHA256("secret", ts+"GET/api/v5/account/balance")
	// Decoded length = 32 (SHA256 digest).
	dec, err := base64.StdEncoding.DecodeString(sig)
	if err != nil || len(dec) != 32 {
		t.Errorf("expected 32-byte decoded base64, got %d (%v)", len(dec), err)
	}
}

func TestRoundContractsToLot(t *testing.T) {
	if got := roundContractsToLot(12.345, 0.1); got != 12.3 {
		t.Errorf("expected 12.3, got %v", got)
	}
	if got := roundContractsToLot(0.05, 1); got != 0 {
		t.Errorf("expected 0, got %v", got)
	}
	if got := roundContractsToLot(5, 0); got != 5 {
		t.Errorf("expected 5 (no step), got %v", got)
	}
}

func TestParseError_Friendly(t *testing.T) {
	body := []byte(`{"code":"51008","msg":"insufficient margin"}`)
	te := parseError(http.StatusOK, body)
	if !strings.Contains(te.Message, "Insufficient margin") {
		t.Errorf("expected friendly mapped, got %q", te.Message)
	}
}

func TestParseError_NestedSubCode(t *testing.T) {
	// OKX often puts the real error in data[0].sCode/sMsg with top-level code="0".
	body := []byte(`{"code":"1","msg":"","data":[{"sCode":"51121","sMsg":"all ops failed"}]}`)
	te := parseError(http.StatusOK, body)
	if te.Code != "51121" {
		t.Errorf("expected sCode promoted to top, got %q", te.Code)
	}
	if !strings.Contains(te.Message, "All operations") {
		t.Errorf("expected friendly mapped, got %q", te.Message)
	}
}

func TestParseError_RateLimit(t *testing.T) {
	body := []byte(`{"code":"50011","msg":"too many"}`)
	te := parseError(http.StatusOK, body)
	if te.Kind != trade.KindRateLimit {
		t.Errorf("expected rate-limit, got %s", te.Kind)
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
		case strings.Contains(r.URL.Path, "/public/instruments"):
			io.WriteString(w, `{"code":"0","data":[
				{"instId":"BTC-USDT-SWAP","lotSz":"1","minSz":"1","tickSz":"0.1","ctVal":"0.01"}
			]}`)
		case strings.HasSuffix(r.URL.Path, "/api/v5/account/set-position-mode"):
			io.WriteString(w, `{"code":"0","data":[]}`)
		case strings.HasSuffix(r.URL.Path, "/api/v5/account/set-leverage"):
			io.WriteString(w, `{"code":"0","data":[]}`)
		case strings.HasSuffix(r.URL.Path, "/api/v5/trade/order"):
			// Verify required headers
			for _, h := range []string{"OK-ACCESS-KEY", "OK-ACCESS-SIGN", "OK-ACCESS-TIMESTAMP", "OK-ACCESS-PASSPHRASE"} {
				if r.Header.Get(h) == "" {
					t.Errorf("missing header %s", h)
				}
			}
			body, _ := io.ReadAll(r.Body)
			if !strings.Contains(string(body), `"instId":"BTC-USDT-SWAP"`) {
				t.Errorf("body missing instId: %s", body)
			}
			if !strings.Contains(string(body), `"posSide":"long"`) {
				t.Errorf("body missing posSide=long: %s", body)
			}
			io.WriteString(w, `{"code":"0","data":[{"ordId":"okx-123","clOrdId":"x"}]}`)
		default:
			t.Errorf("unexpected path %s", r.URL.Path)
			http.Error(w, "?", http.StatusNotFound)
		}
	})
	defer cleanup()

	// 0.50 BTC at 0.01 BTC ctVal = 50 contracts. lotSize=1 → no rounding.
	res, err := a.PlaceOrder(context.Background(),
		trade.Creds{APIKey: "k", APISecret: "s", Passphrase: "p"},
		trade.OpenRequest{
			Symbol: "BTC", Side: trade.SideBuy, Quantity: 0.5,
			Leverage: 5, MarginMode: trade.MarginIsolated,
		})
	if err != nil {
		t.Fatalf("PlaceOrder failed: %v", err)
	}
	if res.OrderID != "okx-123" {
		t.Errorf("got orderId %q", res.OrderID)
	}
	// Quantity in coins should be 50 contracts × 0.01 ctVal = 0.5
	if res.Quantity != 0.5 {
		t.Errorf("got quantity %v, want 0.5", res.Quantity)
	}
}

func TestPlaceOrder_RequiresPassphrase(t *testing.T) {
	a := New()
	_, err := a.PlaceOrder(context.Background(),
		trade.Creds{APIKey: "k", APISecret: "s"}, // no passphrase
		trade.OpenRequest{
			Symbol: "BTC", Side: trade.SideBuy, Quantity: 0.5,
			Leverage: 5, MarginMode: trade.MarginIsolated,
		})
	if err == nil || !trade.IsUser(err) {
		t.Errorf("expected user error for missing passphrase, got %+v", err)
	}
}

func TestRegisteredViaInit(t *testing.T) {
	a := trade.Lookup("okx")
	if a == nil {
		t.Fatal("okx adapter not registered")
	}
	if a.Name() != "okx" {
		t.Errorf("got name %q", a.Name())
	}
}
