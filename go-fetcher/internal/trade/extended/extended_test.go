package extended

import (
	"math/big"
	"strings"
	"testing"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

func TestSymbolNormalization(t *testing.T) {
	cases := map[string]string{
		"btc":     "BTC-USD",
		"ETH":     "ETH-USD",
		"BTC-USD": "BTC-USD",
		"  ena  ": "ENA-USD",
	}
	for in, want := range cases {
		if got := toMarket(in); got != want {
			t.Errorf("toMarket(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestRegisteredViaInit(t *testing.T) {
	a := trade.Lookup("extended")
	if a == nil {
		t.Fatal("extended adapter not registered via init()")
	}
	if a.Name() != "extended" {
		t.Errorf("Name() = %q, want extended", a.Name())
	}
}

func TestEncodeShortString(t *testing.T) {
	// "Perpetuals" — 10 ASCII chars, big-endian byte interpretation
	got := encodeShortString("Perpetuals")
	want := new(big.Int).SetBytes([]byte("Perpetuals"))
	if got.BigInt(new(big.Int)).Cmp(want) != 0 {
		t.Errorf("encodeShortString(Perpetuals) bigint mismatch")
	}
}

func TestSignedFelt_Negative(t *testing.T) {
	// Sanity: negative big.Int → felt should reduce mod P, NOT lose magnitude.
	// We don't assert a specific value but verify it doesn't crash + produces
	// a non-zero felt representation for small negative inputs.
	bi := big.NewInt(-12345)
	f := signedFelt(bi)
	if f.IsZero() {
		t.Fatal("signedFelt(-12345) returned zero felt")
	}
	if f.Cmp(signedFelt(big.NewInt(0))) == 0 {
		t.Fatal("signedFelt(-12345) equal to zero")
	}
}

func TestSignOrder_DeterministicShape(t *testing.T) {
	// We don't have a Python parity vector yet — this test only proves the
	// signing function returns plausible decimal-string r/s pairs without
	// crashing for a representative input. First real testnet order is the
	// truth check (same as Paradex).
	priv := "0x123456789abcdef"
	pub := "0xabcdef0123456789"
	syntheticID, _ := new(big.Int).SetString("1", 10)
	collateralID, _ := new(big.Int).SetString("2", 10)

	sf := func(b *big.Int) string { return b.String() }
	_ = sf
	rDec, sDec, err := signOrder(
		priv,
		1234,
		signedFelt(syntheticID), big.NewInt(1_000_000),
		signedFelt(collateralID), big.NewInt(-50_000_000),
		1000, signedFelt(collateralID),
		1700000000, 42,
		pub,
	)
	if err != nil {
		t.Fatalf("signOrder failed: %v", err)
	}
	if rDec == "" || sDec == "" {
		t.Fatal("signOrder returned empty r/s")
	}
	if strings.HasPrefix(rDec, "-") || strings.HasPrefix(sDec, "-") {
		t.Errorf("signature components should be non-negative decimals; got r=%s s=%s", rDec, sDec)
	}
}

func TestMulResolution(t *testing.T) {
	cases := []struct {
		v          float64
		resolution int64
		want       string
	}{
		{1.5, 1_000_000, "1500000"},
		{0.001, 1_000_000, "1000"},
		{100, 1, "100"},
		{0.123456789, 1_000_000, "123456"},
		{0.2, 1000, "200"},     // SOL syntheticResolution=1000
		{17.30, 1_000_000, "17300000"}, // USDC collateralResolution=1e6
	}
	for _, c := range cases {
		got := mulResolution(c.v, c.resolution).String()
		if got != c.want {
			t.Errorf("mulResolution(%v, %d) = %s, want %s", c.v, c.resolution, got, c.want)
		}
	}
}

func TestParseError_Variants(t *testing.T) {
	tests := []struct {
		status int
		body   []byte
		want   trade.ErrorKind
	}{
		{429, []byte(`{"error":{"message":"too many"}}`), trade.KindRateLimit},
		{401, []byte(`{"error":{"message":"bad key"}}`), trade.KindUser},
		{404, []byte(`{"error":{"message":"not found"}}`), trade.KindUser},
		{503, []byte(`upstream`), trade.KindTransient},
	}
	for _, tc := range tests {
		te := parseError(tc.status, tc.body)
		if te.Kind != tc.want {
			t.Errorf("parseError(%d) kind = %s, want %s", tc.status, te.Kind, tc.want)
		}
	}
}
