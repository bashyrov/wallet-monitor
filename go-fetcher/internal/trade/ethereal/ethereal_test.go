package ethereal

import (
	"encoding/hex"
	"fmt"
	"strings"
	"testing"

	"github.com/ethereum/go-ethereum/crypto"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

func TestSignPersonal_Recoverable(t *testing.T) {
	priv, err := crypto.GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	privHex := hex.EncodeToString(crypto.FromECDSA(priv))
	signerAddr := crypto.PubkeyToAddress(priv.PublicKey).Hex()

	payload := `POST/v1/order123456789{"symbol":"BTC","side":"buy"}`
	sigHex, err := signPersonal(payload, privHex)
	if err != nil {
		t.Fatalf("sign: %v", err)
	}
	if !strings.HasPrefix(sigHex, "0x") || len(sigHex) != 132 {
		t.Fatalf("unexpected sig shape: %q", sigHex)
	}

	prefix := []byte(fmt.Sprintf("\x19Ethereum Signed Message:\n%d", len(payload)))
	digest := crypto.Keccak256(append(prefix, []byte(payload)...))

	sig, _ := hex.DecodeString(strings.TrimPrefix(sigHex, "0x"))
	if sig[64] < 27 {
		t.Fatalf("v not normalized: got %d", sig[64])
	}
	sig[64] -= 27
	pubBytes, err := crypto.Ecrecover(digest, sig)
	if err != nil {
		t.Fatalf("ecrecover: %v", err)
	}
	pub, err := crypto.UnmarshalPubkey(pubBytes)
	if err != nil {
		t.Fatal(err)
	}
	got := crypto.PubkeyToAddress(*pub).Hex()
	if got != signerAddr {
		t.Errorf("recovered %s, signer %s", got, signerAddr)
	}
}

func TestExtractOrderID(t *testing.T) {
	tests := []struct {
		name string
		body string
		want string
	}{
		{"orderId-string", `{"orderId":"abc123"}`, "abc123"},
		{"orderId-number", `{"orderId":42}`, "42"},
		{"id-fallback", `{"id":"xyz"}`, "xyz"},
		{"empty", `{}`, ""},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := extractOrderID([]byte(tc.body)); got != tc.want {
				t.Errorf("got %q want %q", got, tc.want)
			}
		})
	}
}

func TestRegisteredViaInit(t *testing.T) {
	a := trade.Lookup("ethereal")
	if a == nil {
		t.Fatal("ethereal adapter not registered")
	}
}
