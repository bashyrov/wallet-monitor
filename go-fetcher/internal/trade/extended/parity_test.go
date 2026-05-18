package extended

import (
	"math/big"
	"testing"

	"github.com/NethermindEth/juno/core/felt"
	"github.com/NethermindEth/starknet.go/curve"
)

// TestRustParity_OrderMessageHash pins our hash output against the
// reference vector from x10's rust-crypto-lib-base/starknet_messages.rs
// test_message_hash_order. If this fails we've drifted from the canonical
// signing scheme and the venue will reject every order.
//
// Reference (SEPOLIA domain, NOT mainnet):
//
//	position_id=1, base_id=2, base_amt=3, quote_id=4, quote_amt=5,
//	fee_id=6, fee_amt=7, expiration=8, salt=9
//	user_pubkey=15284…6538
//	expected_message_hash = 27889…9597
func TestRustParity_OrderMessageHash(t *testing.T) {
	mustDec := func(s string) *felt.Felt {
		bi, _ := new(big.Int).SetString(s, 10)
		return new(felt.Felt).SetBigInt(bi)
	}
	sepDomainHash := curve.PoseidonArray(
		domainSelector,
		encodeShortString("Perpetuals"),
		encodeShortString("v0"),
		encodeShortString("SN_SEPOLIA"),
		new(felt.Felt).SetUint64(1),
	)
	starkMsg := encodeShortString("StarkNet Message")

	orderHash := curve.PoseidonArray(
		orderSelector,
		new(felt.Felt).SetUint64(1),
		mustDec("2"),
		signedFelt(big.NewInt(3)),
		mustDec("4"),
		signedFelt(big.NewInt(5)),
		mustDec("6"),
		new(felt.Felt).SetUint64(7),
		new(felt.Felt).SetUint64(8),
		mustDec("9"),
	)

	userKey := mustDec("1528491859474308181214583355362479091084733880193869257167008343298409336538")
	msgHash := curve.PoseidonArray(starkMsg, sepDomainHash, userKey, orderHash)
	wantFelt := mustDec("2788960362996410178586013462192086205585543858281504820767681025777602529597")

	if !msgHash.Equal(wantFelt) {
		t.Errorf("Rust parity mismatch:\n  got:  %s\n  want: %s\n  order_hash: %s\n  domain_hash: %s",
			msgHash.String(), wantFelt.String(), orderHash.String(), sepDomainHash.String())
	}
}
