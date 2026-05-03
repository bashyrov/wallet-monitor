package paradex

import (
	"encoding/json"
	"math/big"
	"strings"
	"testing"

	"github.com/NethermindEth/juno/core/felt"
	"github.com/NethermindEth/starknet.go/curve"
	"github.com/NethermindEth/starknet.go/typeddata"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

// TestChainIDFelt pins the felt-encoded chain id we send in the
// SNIP-12 domain. Drift here = every signature mismatches.
//
//	int.from_bytes(b"PRIVATE_SN_PARACLEAR_MAINNET", "big") == ?
//
// We compute the same value and assert the hex form matches.
func TestChainIDFelt(t *testing.T) {
	want := "0x" + new(big.Int).SetBytes([]byte("PRIVATE_SN_PARACLEAR_MAINNET")).Text(16)
	if chainIDHex != want {
		t.Errorf("chainIDHex = %q want %q", chainIDHex, want)
	}
}

// TestAuthTypedData_Roundtrip ensures the auth typed-data we build can
// be parsed by starknet.go's typeddata package and produces a felt
// hash. That validates the JSON shape (field names, types, primary
// type) is acceptable per SNIP-12.
func TestAuthTypedData_Roundtrip(t *testing.T) {
	const address = "0x05c74db20fa8f151bfd3a7a462cf2e8d4578a88aa4bd7a1746955201c48d8e5e"
	tdJSON := buildAuthMessage(1700000000, 1700086400)
	var td typeddata.TypedData
	if err := json.Unmarshal(tdJSON, &td); err != nil {
		t.Fatalf("typed data unmarshal: %v", err)
	}
	hash, err := td.GetMessageHash(address)
	if err != nil {
		t.Fatalf("get hash: %v", err)
	}
	if hash == nil || hash.IsZero() {
		t.Fatalf("hash unexpectedly zero")
	}
}

// TestSign_Recoverable signs a typed-data hash with a known Stark
// private key and verifies the signature recovers/verifies under
// curve.VerifyFelts. Internal consistency only — does NOT prove the
// hash matches paradex-py (that needs a live test vector).
func TestSign_Recoverable(t *testing.T) {
	// Deterministic key.
	privBig, _ := new(big.Int).SetString("0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", 16)
	privFelt := new(felt.Felt).SetBigInt(privBig)
	pubX, _ := curve.PrivateKeyToPoint(privBig)

	tdJSON := buildAuthMessage(1700000000, 1700086400)
	const address = "0x012345abcdef"
	rDec, sDec, err := signTypedData(tdJSON, address, "0x"+privBig.Text(16))
	if err != nil {
		t.Fatalf("sign: %v", err)
	}
	rBig, ok1 := new(big.Int).SetString(rDec, 10)
	sBig, ok2 := new(big.Int).SetString(sDec, 10)
	if !ok1 || !ok2 {
		t.Fatalf("non-decimal signature: r=%q s=%q", rDec, sDec)
	}

	var td typeddata.TypedData
	_ = json.Unmarshal(tdJSON, &td)
	hash, _ := td.GetMessageHash(address)

	rFelt := new(felt.Felt).SetBigInt(rBig)
	sFelt := new(felt.Felt).SetBigInt(sBig)
	pubFelt := new(felt.Felt).SetBigInt(pubX)
	ok, err := curve.VerifyFelts(hash, rFelt, sFelt, pubFelt)
	if err != nil {
		t.Fatalf("verify: %v", err)
	}
	if !ok {
		t.Errorf("signature failed Stark verify")
	}
	_ = privFelt
}

// TestFlattenSignature pins the exact flattening format Paradex
// expects in its PARADEX-STARKNET-SIGNATURE header.
func TestFlattenSignature(t *testing.T) {
	got := flattenSignature("12345", "67890")
	want := `["12345","67890"]`
	if got != want {
		t.Errorf("got %q want %q", got, want)
	}
}

// TestChainQuantum pins the 8-decimal scaling Paradex uses for size /
// price felts.
func TestChainQuantum(t *testing.T) {
	tests := []struct {
		in   float64
		want string
	}{
		{0.001, "100000"},
		{1, "100000000"},
		{1.5, "150000000"},
		{12345.6789, "1234567890000"},
	}
	for _, tc := range tests {
		if got := chainQuantum(tc.in); got != tc.want {
			t.Errorf("chainQuantum(%v) = %q want %q", tc.in, got, tc.want)
		}
	}
}

// TestToParadexMarket pins the symbol mapping.
func TestToParadexMarket(t *testing.T) {
	if got := toParadexMarket("btc"); got != "BTC-USD-PERP" {
		t.Errorf("got %q", got)
	}
}

// TestExtractOrderID covers both `"id":"..."` and bare-number forms.
func TestExtractOrderID(t *testing.T) {
	cases := map[string]string{
		`{"id":"abc123"}`: "abc123",
		`{"id":42}`:       "42",
		`{}`:              "",
	}
	for body, want := range cases {
		if got := extractOrderID([]byte(body)); got != want {
			t.Errorf("body=%s: got %q want %q", body, got, want)
		}
	}
}

func TestRegisteredViaInit(t *testing.T) {
	a := trade.Lookup("paradex")
	if a == nil {
		t.Fatal("paradex adapter not registered")
	}
}

// Sanity: sign + verify via the felt-friendly signing path inside
// signTypedData — make sure decimal output is not accidentally hex.
func TestSignTypedData_DecimalOutput(t *testing.T) {
	priv := "0x" + strings.Repeat("11", 32)
	tdJSON := buildAuthMessage(1, 2)
	r, s, err := signTypedData(tdJSON, "0xab", priv)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := new(big.Int).SetString(r, 10); !ok {
		t.Errorf("r not decimal: %q", r)
	}
	if _, ok := new(big.Int).SetString(s, 10); !ok {
		t.Errorf("s not decimal: %q", s)
	}
}
