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

// TestOrderMessage_Roundtrip ensures the order TypedData we construct is
// parseable by starknet.go and produces a non-zero felt hash. The auth
// message has its own roundtrip test; until now `buildOrderMessage` was
// only exercised by callers, never asserted.
func TestOrderMessage_Roundtrip(t *testing.T) {
	const address = "0x05c74db20fa8f151bfd3a7a462cf2e8d4578a88aa4bd7a1746955201c48d8e5e"
	tdJSON := buildOrderMessage(
		1700000000000,    // signature_timestamp_ms
		"BTC-USD-PERP",   // market
		"1",              // side: BUY
		"MARKET",         // orderType
		"100000",         // chainSize: 0.001 × 1e8
		"0",              // chainPrice: market order
	)
	var tdv typeddata.TypedData
	if err := json.Unmarshal(tdJSON, &tdv); err != nil {
		t.Fatalf("order TypedData unmarshal: %v", err)
	}
	hash, err := tdv.GetMessageHash(address)
	if err != nil {
		t.Fatalf("get hash: %v", err)
	}
	if hash == nil || hash.IsZero() {
		t.Fatalf("order hash unexpectedly zero")
	}
}

// TestSignOrder_Recoverable signs a real order TypedData and verifies
// via Stark VerifyFelts. Without this we'd have NO assurance that
// signTypedData produces valid sigs over order messages — only auth.
func TestSignOrder_Recoverable(t *testing.T) {
	privBig, _ := new(big.Int).SetString("0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", 16)
	pubX, _ := curve.PrivateKeyToPoint(privBig)
	const address = "0x012345abcdef"

	tdJSON := buildOrderMessage(1700000000000, "BTC-USD-PERP", "1", "MARKET", "100000", "0")
	rDec, sDec, err := signTypedData(tdJSON, address, "0x"+privBig.Text(16))
	if err != nil {
		t.Fatalf("sign: %v", err)
	}
	rBig, _ := new(big.Int).SetString(rDec, 10)
	sBig, _ := new(big.Int).SetString(sDec, 10)

	var tdv typeddata.TypedData
	_ = json.Unmarshal(tdJSON, &tdv)
	hash, _ := tdv.GetMessageHash(address)

	rFelt := new(felt.Felt).SetBigInt(rBig)
	sFelt := new(felt.Felt).SetBigInt(sBig)
	pubFelt := new(felt.Felt).SetBigInt(pubX)
	ok, err := curve.VerifyFelts(hash, rFelt, sFelt, pubFelt)
	if err != nil {
		t.Fatalf("verify: %v", err)
	}
	if !ok {
		t.Errorf("order signature failed Stark verify")
	}
}

// TestAuthMessage_HashPinned pins the SNIP-12 hash that starknet.go
// produces for our auth TypedData against a fixed (timestamp, expiration,
// address). If a starknet.go upgrade silently changes the hash output —
// e.g. revision-handling tweak, encoding change — this test catches it
// before we ship and Paradex starts rejecting our sessions.
//
// Vector locked at starknet.go v0.17.1 / juno v0.15.11.
func TestAuthMessage_HashPinned(t *testing.T) {
	const address = "0x05c74db20fa8f151bfd3a7a462cf2e8d4578a88aa4bd7a1746955201c48d8e5e"
	tdJSON := buildAuthMessage(1700000000, 1700086400)
	var tdv typeddata.TypedData
	if err := json.Unmarshal(tdJSON, &tdv); err != nil {
		t.Fatal(err)
	}
	hash, err := tdv.GetMessageHash(address)
	if err != nil {
		t.Fatal(err)
	}
	const want = "0x394b78da39c8e86c548e3b41ba3233fb135a06095c65eb8a1ca06a56e37728f"
	if got := hash.String(); got != want {
		t.Errorf("hash drift\n got  %s\n want %s\n(starknet.go upgrade may have changed SNIP-12 encoding)", got, want)
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
