package upbit

import (
	"crypto/hmac"
	"crypto/sha256"
	"crypto/sha512"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"strings"
	"testing"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

func TestRegisteredViaInit(t *testing.T) {
	a := trade.Lookup("upbit")
	if a == nil {
		t.Fatal("upbit adapter not registered")
	}
	if _, ok := a.(trade.SpotAdapter); !ok {
		t.Fatal("upbit must implement SpotAdapter (spot-native venue)")
	}
}

func TestBuildJWT(t *testing.T) {
	params := []param{{"market", "USDT-BTC"}, {"side", "bid"}, {"ord_type", "price"}, {"price", "600.00000000"}}
	tok := buildJWT("test-key", "test-secret", params)

	parts := strings.Split(tok, ".")
	if len(parts) != 3 {
		t.Fatalf("JWT must have 3 segments, got %d", len(parts))
	}
	hdr, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil || string(hdr) != `{"alg":"HS256","typ":"JWT"}` {
		t.Fatalf("header wrong: %s %v", hdr, err)
	}
	pj, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		t.Fatalf("payload decode: %v", err)
	}
	var payload struct {
		AccessKey    string `json:"access_key"`
		Nonce        string `json:"nonce"`
		QueryHash    string `json:"query_hash"`
		QueryHashAlg string `json:"query_hash_alg"`
	}
	if err := json.Unmarshal(pj, &payload); err != nil {
		t.Fatalf("payload json: %v", err)
	}
	if payload.AccessKey != "test-key" {
		t.Errorf("access_key = %q", payload.AccessKey)
	}
	if len(payload.Nonce) != 36 || strings.Count(payload.Nonce, "-") != 4 {
		t.Errorf("nonce not UUID4-shaped: %q", payload.Nonce)
	}
	if payload.QueryHashAlg != "SHA512" {
		t.Errorf("query_hash_alg = %q", payload.QueryHashAlg)
	}
	want := sha512.Sum512([]byte("market=USDT-BTC&side=bid&ord_type=price&price=600.00000000"))
	if payload.QueryHash != hex.EncodeToString(want[:]) {
		t.Errorf("query_hash mismatch:\n got %s\nwant %s", payload.QueryHash, hex.EncodeToString(want[:]))
	}
	mac := hmac.New(sha256.New, []byte("test-secret"))
	mac.Write([]byte(parts[0] + "." + parts[1]))
	if parts[2] != base64.RawURLEncoding.EncodeToString(mac.Sum(nil)) {
		t.Error("HS256 signature does not verify")
	}
}

func TestBuildJWT_NoParamsOmitsQueryHash(t *testing.T) {
	tok := buildJWT("k", "s", nil)
	pj, _ := base64.RawURLEncoding.DecodeString(strings.Split(tok, ".")[1])
	if strings.Contains(string(pj), "query_hash") {
		t.Errorf("parameterless request must omit query_hash: %s", pj)
	}
}

func TestOrderParams_AsymmetricMarketOrders(t *testing.T) {
	// BUY spends quote: ord_type="price", price = qty × last.
	buy := orderParams("btc", true, 0.01, 60000)
	wantBuy := []param{
		{"market", "USDT-BTC"}, {"side", "bid"}, {"ord_type", "price"}, {"price", "600.00000000"},
	}
	if len(buy) != len(wantBuy) {
		t.Fatalf("buy params: %+v", buy)
	}
	for i := range wantBuy {
		if buy[i] != wantBuy[i] {
			t.Errorf("buy[%d] = %+v, want %+v", i, buy[i], wantBuy[i])
		}
	}
	// SELL sends base volume: ord_type="market".
	sell := orderParams("BTC", false, 0.01, 0)
	wantSell := []param{
		{"market", "USDT-BTC"}, {"side", "ask"}, {"ord_type", "market"}, {"volume", "0.01000000"},
	}
	for i := range wantSell {
		if sell[i] != wantSell[i] {
			t.Errorf("sell[%d] = %+v, want %+v", i, sell[i], wantSell[i])
		}
	}
}

func TestEncodeBody_PreservesParamOrder(t *testing.T) {
	// Hash preimage and JSON body must be built from the same ordered
	// slice — Upbit validates query_hash against the request params.
	b := encodeBody([]param{{"market", "USDT-BTC"}, {"side", "ask"}, {"ord_type", "market"}, {"volume", "1"}})
	want := `{"market":"USDT-BTC","side":"ask","ord_type":"market","volume":"1"}`
	if string(b) != want {
		t.Errorf("body = %s, want %s", b, want)
	}
}
