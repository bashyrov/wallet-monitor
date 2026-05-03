package hyperliquid

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math/big"
	"strings"
	"testing"

	"github.com/ethereum/go-ethereum/crypto"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

func TestSignAction_Recoverable(t *testing.T) {
	priv, err := crypto.GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	privHex := hex.EncodeToString(crypto.FromECDSA(priv))
	signerAddr := crypto.PubkeyToAddress(priv.PublicKey).Hex()

	action := map[string]any{
		"type":     "order",
		"orders":   []map[string]any{{"a": 0, "b": true, "p": "0", "s": "0.001", "r": false}},
		"grouping": "na",
		"nonce":    int64(1700000000000),
	}
	r, s, v, err := signAction(action, privHex)
	if err != nil {
		t.Fatalf("sign: %v", err)
	}
	if v != 27 && v != 28 {
		t.Errorf("v should be 27/28, got %d", v)
	}

	// Reconstruct the same digest the signer used.
	canon, _ := json.Marshal(action)
	hashHex := sha256.Sum256(canon)
	hexBytes := hex.EncodeToString(hashHex[:])
	prefix := []byte(fmt.Sprintf("\x19Ethereum Signed Message:\n%d", len(hexBytes)))
	digest := crypto.Keccak256(append(prefix, []byte(hexBytes)...))

	rBig, _ := new(big.Int).SetString(strings.TrimPrefix(r, "0x"), 16)
	sBig, _ := new(big.Int).SetString(strings.TrimPrefix(s, "0x"), 16)
	sig := make([]byte, 65)
	rBytes := rBig.Bytes()
	sBytes := sBig.Bytes()
	copy(sig[32-len(rBytes):32], rBytes)
	copy(sig[64-len(sBytes):64], sBytes)
	sig[64] = byte(v - 27)

	pubBytes, err := crypto.Ecrecover(digest, sig)
	if err != nil {
		t.Fatalf("ecrecover: %v", err)
	}
	pub, err := crypto.UnmarshalPubkey(pubBytes)
	if err != nil {
		t.Fatal(err)
	}
	if got := crypto.PubkeyToAddress(*pub).Hex(); got != signerAddr {
		t.Errorf("recovered %s, signer %s", got, signerAddr)
	}
}

func TestExtractOrderID_Resting(t *testing.T) {
	body := []byte(`{"status":"ok","response":{"data":{"statuses":[{"resting":{"oid":12345}}]}}}`)
	if got := extractOrderID(body); got != "12345" {
		t.Errorf("got %q", got)
	}
}

func TestExtractOrderID_Filled(t *testing.T) {
	body := []byte(`{"status":"ok","response":{"data":{"statuses":[{"filled":{"oid":7}}]}}}`)
	if got := extractOrderID(body); got != "7" {
		t.Errorf("got %q", got)
	}
}

func TestRegisteredViaInit(t *testing.T) {
	a := trade.Lookup("hyperliquid")
	if a == nil {
		t.Fatal("hyperliquid adapter not registered")
	}
}
