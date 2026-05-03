package aster

import (
	"crypto/ecdsa"
	"encoding/hex"
	"strings"
	"testing"

	"github.com/ethereum/go-ethereum/crypto"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

func TestSymbol(t *testing.T) {
	if got := toAsterSymbol("btc"); got != "BTCUSDT" {
		t.Errorf("got %q", got)
	}
	if got := toAsterSymbol("ETH"); got != "ETHUSDT" {
		t.Errorf("got %q", got)
	}
}

func TestBuildQueryString_Sorted(t *testing.T) {
	got := buildQueryString(map[string]string{
		"symbol":   "BTCUSDT",
		"side":     "BUY",
		"type":     "MARKET",
		"quantity": "0.001",
	})
	want := "quantity=0.001&side=BUY&symbol=BTCUSDT&type=MARKET"
	if got != want {
		t.Errorf("got %q\nwant %q", got, want)
	}
}

// TestSignEIP712_Recoverable verifies the signature is recoverable to
// the signer's public key. Aster's API doesn't publish a fixed test
// vector, so we round-trip: sign → recover → expect the signer's
// derived address back.
func TestSignEIP712_Recoverable(t *testing.T) {
	priv, err := crypto.GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	privHex := hex.EncodeToString(crypto.FromECDSA(priv))
	signerAddr := crypto.PubkeyToAddress(priv.PublicKey).Hex()

	qs := "quantity=0.001&side=BUY&symbol=BTCUSDT&type=MARKET"
	sigHex, err := signEIP712(qs, privHex)
	if err != nil {
		t.Fatalf("sign: %v", err)
	}
	if !strings.HasPrefix(sigHex, "0x") || len(sigHex) != 132 {
		t.Fatalf("unexpected sig shape: %q (len=%d)", sigHex, len(sigHex))
	}

	// Recompute the digest the way signEIP712 did so we can recover.
	digest := mustEIP712Digest(t, qs)
	sig, err := hex.DecodeString(strings.TrimPrefix(sigHex, "0x"))
	if err != nil {
		t.Fatal(err)
	}
	if sig[64] < 27 {
		t.Fatalf("v not normalized to 27/28: got %d", sig[64])
	}
	sig[64] -= 27 // crypto.Ecrecover wants v ∈ {0,1}

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
	_ = (*ecdsa.PrivateKey)(priv) // keep ecdsa import used
}

// mustEIP712Digest mirrors the digest construction inside signEIP712
// without running through the signer.
func mustEIP712Digest(t *testing.T, qs string) []byte {
	t.Helper()
	// We re-use the production signer by grabbing the pre-image digest
	// indirectly: produce a sig with a fresh key and verify length only.
	// The actual digest reconstruction below mirrors signEIP712.
	const (
		domainTypeHashHex = ""
	)
	// Re-derive via a temporary call is awkward; rebuild manually.
	domainSep := keccak(append(
		keccak([]byte("EIP712Domain(string name,uint256 chainId)")),
		append(keccak([]byte("AsterSignTransaction")), pad32Big(chainID)...)...,
	))
	msgHash := keccak(append(
		keccak([]byte("AsterSignTransaction(string params)")),
		keccak([]byte(qs))...,
	))
	raw := append([]byte{0x19, 0x01}, domainSep...)
	raw = append(raw, msgHash...)
	return keccak(raw)
}

func keccak(b []byte) []byte { return crypto.Keccak256(b) }

func pad32Big(n int) []byte {
	out := make([]byte, 32)
	// big-endian for chainId
	for i := 0; i < 8 && n > 0; i++ {
		out[31-i] = byte(n & 0xff)
		n >>= 8
	}
	return out
}

func TestRegisteredViaInit(t *testing.T) {
	a := trade.Lookup("aster")
	if a == nil {
		t.Fatal("aster adapter not registered")
	}
}
