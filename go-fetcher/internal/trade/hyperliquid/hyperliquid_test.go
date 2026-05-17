package hyperliquid

import (
	"bytes"
	"encoding/binary"
	"encoding/hex"
	"math/big"
	"strings"
	"testing"

	gethmath "github.com/ethereum/go-ethereum/common/math"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/signer/core/apitypes"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

// TestPackAction_PythonParity pins our msgpack output to what
// msgpack-python produces for an identical action. If this byte-string
// drifts, Python and Go signatures will diverge and HL will reject
// every order.
//
// Reference (msgpack-python 1.x):
//   action = {
//     "type": "order",
//     "orders": [{"a": 0, "b": True, "p": "0", "s": "0.001",
//                 "r": False, "t": {"limit": {"tif": "Ioc"}}}],
//     "grouping": "na",
//   }
//   msgpack.packb(action, use_bin_type=True).hex() ==
//     "83a474797065a56f72646572a66f72646572739186a16100a162c3a170" +
//     "a130a173a5302e303031a172c2a17481a56c696d697481a3746966a349" +
//     "6f63a867726f7570696e67a26e61"
func TestPackAction_PythonParity(t *testing.T) {
	action := orderAction{
		Type: "order",
		Orders: []orderLeg{{
			A: 0, B: true, P: "0", S: "0.001", R: false,
			T: orderTypeBox{Limit: orderLimit{Tif: "Ioc"}},
		}},
		Grouping: "na",
	}
	got, err := packAction(action)
	if err != nil {
		t.Fatal(err)
	}
	want := "83a474797065a56f72646572a66f72646572739186a16100a162c3a170a130a173a5302e303031a172c2a17481a56c696d697481a3746966a3496f63a867726f7570696e67a26e61"
	if hex.EncodeToString(got) != want {
		t.Errorf("msgpack mismatch\n got  %x\n want %s", got, want)
	}
}

// TestPackAction_FieldOrder verifies struct field declaration order is
// what msgpack writes. If this fails, every signature mismatches.
func TestPackAction_FieldOrder(t *testing.T) {
	action := orderAction{
		Type: "order",
		Orders: []orderLeg{{
			A: 0, B: true, P: "0", S: "0.001", R: false,
			T: orderTypeBox{Limit: orderLimit{Tif: "Ioc"}},
		}},
		Grouping: "na",
	}
	packed, err := packAction(action)
	if err != nil {
		t.Fatal(err)
	}
	// We don't pin to a hex string (msgpack-python equivalence is the
	// real check, run separately) but we check the keys appear in the
	// expected order: type → orders → grouping.
	idxType := bytes.Index(packed, []byte("type"))
	idxOrders := bytes.Index(packed, []byte("orders"))
	idxGrouping := bytes.Index(packed, []byte("grouping"))
	if idxType < 0 || idxOrders < 0 || idxGrouping < 0 {
		t.Fatalf("missing key in packed: %x", packed)
	}
	if !(idxType < idxOrders && idxOrders < idxGrouping) {
		t.Errorf("field order wrong: type=%d orders=%d grouping=%d", idxType, idxOrders, idxGrouping)
	}
	// Order leg subkeys: a → b → p → s → r → t
	idxA := bytes.Index(packed, []byte("\xa1a"))
	idxB := bytes.Index(packed, []byte("\xa1b"))
	idxP := bytes.Index(packed, []byte("\xa1p"))
	idxS := bytes.Index(packed, []byte("\xa1s"))
	idxR := bytes.Index(packed, []byte("\xa1r"))
	idxT := bytes.Index(packed, []byte("\xa1t"))
	if !(idxA < idxB && idxB < idxP && idxP < idxS && idxS < idxR && idxR < idxT) {
		t.Errorf("order-leg field order wrong: a=%d b=%d p=%d s=%d r=%d t=%d", idxA, idxB, idxP, idxS, idxR, idxT)
	}
}

// TestSignPhantomAgent_Recoverable verifies the EIP-712 sig recovers
// to the agent address. Internal-consistency check; doesn't validate
// against HL's verifier (that requires a live testnet order).
func TestSignPhantomAgent_Recoverable(t *testing.T) {
	priv, err := crypto.GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	privHex := hex.EncodeToString(crypto.FromECDSA(priv))
	signerAddr := crypto.PubkeyToAddress(priv.PublicKey).Hex()

	action := orderAction{
		Type: "order",
		Orders: []orderLeg{{
			A: 0, B: true, P: "0", S: "0.001", R: false,
			T: orderTypeBox{Limit: orderLimit{Tif: "Ioc"}},
		}},
		Grouping: "na",
	}
	packed, err := packAction(action)
	if err != nil {
		t.Fatal(err)
	}
	const nonce int64 = 1700000000000
	r, s, v, err := signPhantomAgent(packed, nonce, "", true, privHex)
	if err != nil {
		t.Fatalf("sign: %v", err)
	}
	if v != 27 && v != 28 {
		t.Errorf("v should be 27/28, got %d", v)
	}

	// Reconstruct the same digest the signer used.
	var buf bytes.Buffer
	buf.Write(packed)
	var nonceBuf [8]byte
	binary.BigEndian.PutUint64(nonceBuf[:], uint64(nonce))
	buf.Write(nonceBuf[:])
	buf.WriteByte(0x00)
	connectionID := crypto.Keccak256(buf.Bytes())

	td := apitypes.TypedData{
		Types: apitypes.Types{
			"EIP712Domain": []apitypes.Type{
				{Name: "name", Type: "string"},
				{Name: "version", Type: "string"},
				{Name: "chainId", Type: "uint256"},
				{Name: "verifyingContract", Type: "address"},
			},
			"Agent": []apitypes.Type{
				{Name: "source", Type: "string"},
				{Name: "connectionId", Type: "bytes32"},
			},
		},
		PrimaryType: "Agent",
		Domain: apitypes.TypedDataDomain{
			Name:              "Exchange",
			Version:           "1",
			ChainId:           gethmath.NewHexOrDecimal256(1337),
			VerifyingContract: "0x0000000000000000000000000000000000000000",
		},
		Message: apitypes.TypedDataMessage{
			"source":       "a",
			"connectionId": connectionID,
		},
	}
	domainSep, _ := td.HashStruct("EIP712Domain", td.Domain.Map())
	msgHash, _ := td.HashStruct("Agent", td.Message)
	digest := crypto.Keccak256(append(append([]byte{0x19, 0x01}, domainSep...), msgHash...))

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

// TestSignPhantomAgent_PythonParity pins our (r,s,v) output to what the
// Python adapter produces for an identical (key, nonce, action). This
// is the load-bearing cross-language check — if it drifts, HL will
// reject every order.
func TestSignPhantomAgent_PythonParity(t *testing.T) {
	const privHex = "1111111111111111111111111111111111111111111111111111111111111111"
	const nonce int64 = 1700000000000
	action := orderAction{
		Type: "order",
		Orders: []orderLeg{{
			A: 0, B: true, P: "0", S: "0.001", R: false,
			T: orderTypeBox{Limit: orderLimit{Tif: "Ioc"}},
		}},
		Grouping: "na",
	}
	packed, err := packAction(action)
	if err != nil {
		t.Fatal(err)
	}
	r, s, v, err := signPhantomAgent(packed, nonce, "", true, privHex)
	if err != nil {
		t.Fatal(err)
	}
	wantR := "0xb3658ae97602ecc18c5c0677d91c9fabab5e1b08ddbe3c45cdee5ebb81b47094"
	wantS := "0x7bbbba914d3d90141ec646c27375189002e5b2da3a5d31afacb2f2f521d2250e"
	wantV := 28
	if r != wantR || s != wantS || v != wantV {
		t.Errorf("sig mismatch\n got  r=%s\n      s=%s\n      v=%d\n want r=%s\n      s=%s\n      v=%d",
			r, s, v, wantR, wantS, wantV)
	}
}

func TestExtractOrderResult_Resting(t *testing.T) {
	body := []byte(`{"status":"ok","response":{"data":{"statuses":[{"resting":{"oid":12345}}]}}}`)
	oid, avg, _ := extractOrderResult(body)
	if oid != "12345" {
		t.Errorf("oid got %q", oid)
	}
	if avg != 0 {
		t.Errorf("avg got %v, want 0", avg)
	}
}

func TestExtractOrderResult_Filled(t *testing.T) {
	body := []byte(`{"status":"ok","response":{"data":{"statuses":[{"filled":{"oid":7,"avgPx":"43000.5"}}]}}}`)
	oid, avg, _ := extractOrderResult(body)
	if oid != "7" {
		t.Errorf("oid got %q", oid)
	}
	if avg != 43000.5 {
		t.Errorf("avg got %v, want 43000.5", avg)
	}
}

// TestSignPhantomAgent_VaultAddress verifies the vault-address branch of
// the action_hash construction (0x01 || bytes20(addr)). Vault subaccounts
// are a real HL feature (institutional users); the codepath wasn't covered
// by the no-vault parity test. We assert the (r,s,v) is recoverable and
// — crucially — DIFFERENT from the no-vault sig over the same action.
func TestSignPhantomAgent_VaultAddress(t *testing.T) {
	priv, err := crypto.GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	privHex := hex.EncodeToString(crypto.FromECDSA(priv))
	signerAddr := crypto.PubkeyToAddress(priv.PublicKey).Hex()

	action := orderAction{
		Type: "order",
		Orders: []orderLeg{{A: 0, B: true, P: "0", S: "0.001", R: false,
			T: orderTypeBox{Limit: orderLimit{Tif: "Ioc"}}}},
		Grouping: "na",
	}
	packed, _ := packAction(action)
	const nonce int64 = 1700000000000
	const vault = "0x1234567890abcdef1234567890abcdef12345678"

	rNoVault, sNoVault, _, err := signPhantomAgent(packed, nonce, "", true, privHex)
	if err != nil {
		t.Fatal(err)
	}
	rVault, sVault, vVault, err := signPhantomAgent(packed, nonce, vault, true, privHex)
	if err != nil {
		t.Fatal(err)
	}
	if rVault == rNoVault && sVault == sNoVault {
		t.Errorf("vault sig should differ from no-vault sig")
	}

	// Reconstruct vault digest and recover signer.
	var buf bytes.Buffer
	buf.Write(packed)
	var nonceBuf [8]byte
	binary.BigEndian.PutUint64(nonceBuf[:], uint64(nonce))
	buf.Write(nonceBuf[:])
	buf.WriteByte(0x01)
	addrBytes, _ := hex.DecodeString(strings.TrimPrefix(vault, "0x"))
	buf.Write(addrBytes)
	connectionID := crypto.Keccak256(buf.Bytes())

	td := apitypes.TypedData{
		Types: apitypes.Types{
			"EIP712Domain": []apitypes.Type{
				{Name: "name", Type: "string"}, {Name: "version", Type: "string"},
				{Name: "chainId", Type: "uint256"}, {Name: "verifyingContract", Type: "address"},
			},
			"Agent": []apitypes.Type{
				{Name: "source", Type: "string"}, {Name: "connectionId", Type: "bytes32"},
			},
		},
		PrimaryType: "Agent",
		Domain: apitypes.TypedDataDomain{
			Name: "Exchange", Version: "1",
			ChainId:           gethmath.NewHexOrDecimal256(1337),
			VerifyingContract: "0x0000000000000000000000000000000000000000",
		},
		Message: apitypes.TypedDataMessage{"source": "a", "connectionId": connectionID},
	}
	domainSep, _ := td.HashStruct("EIP712Domain", td.Domain.Map())
	msgHash, _ := td.HashStruct("Agent", td.Message)
	digest := crypto.Keccak256(append(append([]byte{0x19, 0x01}, domainSep...), msgHash...))

	rBig, _ := new(big.Int).SetString(strings.TrimPrefix(rVault, "0x"), 16)
	sBig, _ := new(big.Int).SetString(strings.TrimPrefix(sVault, "0x"), 16)
	sig := make([]byte, 65)
	rBytes := rBig.Bytes()
	sBytes := sBig.Bytes()
	copy(sig[32-len(rBytes):32], rBytes)
	copy(sig[64-len(sBytes):64], sBytes)
	sig[64] = byte(vVault - 27)
	pubBytes, err := crypto.Ecrecover(digest, sig)
	if err != nil {
		t.Fatalf("ecrecover: %v", err)
	}
	pub, _ := crypto.UnmarshalPubkey(pubBytes)
	if got := crypto.PubkeyToAddress(*pub).Hex(); got != signerAddr {
		t.Errorf("vault sig recovered %s, want %s", got, signerAddr)
	}
}

// TestSignPhantomAgent_Testnet ensures source="b" produces a different
// signature than source="a" — i.e. a testnet sig isn't valid on mainnet
// and vice versa. Otherwise the env flip would silently route orders
// to the wrong network.
func TestSignPhantomAgent_Testnet(t *testing.T) {
	priv, _ := crypto.GenerateKey()
	privHex := hex.EncodeToString(crypto.FromECDSA(priv))

	action := orderAction{
		Type:     "order",
		Orders:   []orderLeg{{A: 0, B: true, P: "0", S: "0.001", R: false, T: orderTypeBox{Limit: orderLimit{Tif: "Ioc"}}}},
		Grouping: "na",
	}
	packed, _ := packAction(action)
	const nonce int64 = 1700000000000

	rMain, sMain, _, _ := signPhantomAgent(packed, nonce, "", true, privHex)
	rTest, sTest, _, _ := signPhantomAgent(packed, nonce, "", false, privHex)
	if rMain == rTest && sMain == sTest {
		t.Errorf("mainnet/testnet sigs must differ — same key would otherwise be replayable across networks")
	}
}

// TestPackUpdateLeverage_FieldOrder pins the msgpack key order for the
// updateLeverage action. Same load-bearing concern as orderAction:
// reorder = signature mismatch.
func TestPackUpdateLeverage_FieldOrder(t *testing.T) {
	action := updateLeverageAction{
		Type: "updateLeverage", Asset: 0, IsCross: true, Leverage: 10,
	}
	packed, err := packAction(action)
	if err != nil {
		t.Fatal(err)
	}
	idxType := bytes.Index(packed, []byte("type"))
	idxAsset := bytes.Index(packed, []byte("asset"))
	idxIsCross := bytes.Index(packed, []byte("isCross"))
	idxLev := bytes.Index(packed, []byte("leverage"))
	if !(idxType < idxAsset && idxAsset < idxIsCross && idxIsCross < idxLev) {
		t.Errorf("updateLeverage field order wrong: type=%d asset=%d isCross=%d leverage=%d",
			idxType, idxAsset, idxIsCross, idxLev)
	}
}

func TestRegisteredViaInit(t *testing.T) {
	a := trade.Lookup("hyperliquid")
	if a == nil {
		t.Fatal("hyperliquid adapter not registered")
	}
}
