// Lighter L2 transaction signing helpers — thin wrappers around the
// official elliottech/lighter-go SDK. Kept in a separate file so the
// dependency surface stays contained.
//
// Schnorr / Poseidon over ECgFp5 curve — pure Go, no CGO.

package lighter

import (
	"encoding/hex"
	"fmt"
	"strings"

	lighter_signer "github.com/elliottech/lighter-go/signer"
	lighter_types "github.com/elliottech/lighter-go/types"
)

// lighterKeyManager parses the 40-byte ECgFp5 private key from a hex
// string. Lighter API keys are 80-hex-char strings; we strip 0x prefix
// if the user pasted it.
func lighterKeyManager(apiSecret string) (lighter_signer.KeyManager, error) {
	s := strings.TrimSpace(apiSecret)
	s = strings.TrimPrefix(s, "0x")
	if len(s) != 80 {
		return nil, fmt.Errorf("lighter: api_secret must be 80 hex chars (got %d)", len(s))
	}
	b, err := hex.DecodeString(s)
	if err != nil {
		return nil, fmt.Errorf("lighter: api_secret not hex: %w", err)
	}
	return lighter_signer.NewKeyManager(b)
}

// lighterConstructOrder builds + signs a CreateOrder L2 tx. Returns the
// SDK's L2CreateOrderTxInfo which serialises directly to the wire shape
// /api/v1/sendTx expects.
func lighterConstructOrder(
	km lighter_signer.KeyManager,
	chainID uint32,
	marketIdx int16,
	baseAmount int64,
	price uint32,
	isAsk uint8,
	orderType uint8,
	accountIdx *int64,
	apiKeyIdx *uint8,
) (any, error) {
	req := &lighter_types.CreateOrderTxReq{
		MarketIndex:      marketIdx,
		ClientOrderIndex: 0, // 0 = auto-assign on Lighter side
		BaseAmount:       baseAmount,
		Price:            price,
		IsAsk:            isAsk,
		Type:             orderType,
		TimeInForce:      0, // 0 = GTC; market orders ignore this
		ReduceOnly:       0,
		TriggerPrice:     0,
		OrderExpiry:      0, // 0 = use DefaultExpireTime
	}
	ops := &lighter_types.TransactOpts{
		FromAccountIndex: accountIdx,
		ApiKeyIndex:      apiKeyIdx,
	}
	tx, err := lighter_types.ConstructCreateOrderTx(km, chainID, req, ops)
	if err != nil {
		return nil, err
	}
	return tx, nil
}
