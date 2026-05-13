package wsbroadcast

import (
	"testing"
)

func TestNormalizePair_Valid(t *testing.T) {
	cases := []struct {
		in, want string
	}{
		{"binance:BTC", "binance:BTC"},
		{"BINANCE:btc", "binance:BTC"},        // case-normalized
		{"  binance  :  BTC  ", "binance:BTC"}, // whitespace trimmed
		{"hyperliquid:ETH", "hyperliquid:ETH"},
		{"okx:BTC_USD_SWAP", ""}, // underscores fine — wait, _ is allowed
	}
	// underscore is allowed per isPairToken — fix expected value
	cases[4].want = "okx:BTC_USD_SWAP"

	for _, c := range cases {
		got, ok := normalizePair(c.in)
		if !ok && c.want != "" {
			t.Errorf("normalizePair(%q) = !ok, want %q", c.in, c.want)
			continue
		}
		if got != c.want {
			t.Errorf("normalizePair(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestNormalizePair_Invalid(t *testing.T) {
	cases := []string{
		"",
		":",
		":BTC",
		"binance:",
		"a/b:BTC",  // slash not in alnum_
		"binance:BTC-USDT", // dash not in alnum_
		"binance:BTC.USD",  // dot not in alnum_
		"x:y:z", // multiple colons — splits at first; "x" : "y:z" — y:z contains : which isn't alnum_
	}
	for _, c := range cases {
		if _, ok := normalizePair(c); ok {
			t.Errorf("normalizePair(%q) should be invalid", c)
		}
	}
}

func TestNormalizePair_LengthCap(t *testing.T) {
	// > 24 chars per side rejected
	tooLong := "binance:" + string(make([]byte, 25))
	if _, ok := normalizePair(tooLong); ok {
		t.Errorf("normalizePair with >24-char symbol should be invalid")
	}
}

func TestSplitPair_RoundTrip(t *testing.T) {
	ex, sym, ok := splitPair("binance:BTC")
	if !ok || ex != "binance" || sym != "BTC" {
		t.Errorf("splitPair: ex=%q sym=%q ok=%v", ex, sym, ok)
	}
}

func TestSplitPair_NoColon(t *testing.T) {
	if _, _, ok := splitPair("binanceBTC"); ok {
		t.Errorf("splitPair without colon should fail")
	}
}

func TestSplitPair_EmptyExOrSym(t *testing.T) {
	if _, _, ok := splitPair(":BTC"); ok {
		t.Errorf("splitPair with empty ex should fail")
	}
	if _, _, ok := splitPair("binance:"); ok {
		t.Errorf("splitPair with empty sym should fail")
	}
}

func TestIsPairToken(t *testing.T) {
	if !isPairToken("binance") {
		t.Error("alnum should be valid")
	}
	if !isPairToken("BTC_USD") {
		t.Error("underscore allowed")
	}
	if isPairToken("BTC-USD") {
		t.Error("dash rejected")
	}
	if isPairToken("BTC.USD") {
		t.Error("dot rejected")
	}
	if isPairToken("BTC/USD") {
		t.Error("slash rejected")
	}
}

func TestBook_HandleSubscribe_CapsAt100Pairs(t *testing.T) {
	b := NewBook(nil, nil, nil)
	// Construct minimal client (no conn — we won't call runReader)
	c := &client{outbox: make(chan []byte, 8), done: make(chan struct{})}
	b.subs[c] = make(map[string]float64, 4)

	// Try to subscribe to 150 pairs — cap is 100
	pairs := make([]string, 150)
	for i := range pairs {
		// distinct symbols so they don't dedupe
		// note: simple decimal encoding stays alnum + underscore
		pairs[i] = "binance:SYM" + intToStr(i)
	}
	b.handleSubscribe(c, pairs)

	if got := len(b.subs[c]); got > bookMaxPairsPerClient {
		t.Errorf("subs cap violated: got %d, max %d", got, bookMaxPairsPerClient)
	}
	if got := len(b.subs[c]); got != bookMaxPairsPerClient {
		t.Errorf("subs should fill to cap: got %d, want %d", got, bookMaxPairsPerClient)
	}
}

func TestBook_HandleSubscribe_DedupesExistingPairs(t *testing.T) {
	b := NewBook(nil, nil, nil)
	c := &client{outbox: make(chan []byte, 8), done: make(chan struct{})}
	b.subs[c] = make(map[string]float64, 4)

	b.handleSubscribe(c, []string{"binance:BTC", "binance:ETH"})
	b.handleSubscribe(c, []string{"binance:BTC", "binance:SOL"}) // BTC duplicate, SOL new

	if got := len(b.subs[c]); got != 3 {
		t.Errorf("after dedupe should have 3 pairs (BTC, ETH, SOL), got %d", got)
	}
}

func TestBook_HandleUnsubscribe_RemovesPairs(t *testing.T) {
	b := NewBook(nil, nil, nil)
	c := &client{outbox: make(chan []byte, 8), done: make(chan struct{})}
	b.subs[c] = make(map[string]float64, 4)

	b.handleSubscribe(c, []string{"binance:BTC", "binance:ETH"})
	b.handleUnsubscribe(c, []string{"binance:BTC"})

	if _, ok := b.subs[c]["binance:BTC"]; ok {
		t.Errorf("BTC still subscribed after unsubscribe")
	}
	if _, ok := b.subs[c]["binance:ETH"]; !ok {
		t.Errorf("ETH should remain")
	}
}

func TestBook_HandleSubscribe_RejectsMalformedPairs(t *testing.T) {
	b := NewBook(nil, nil, nil)
	c := &client{outbox: make(chan []byte, 8), done: make(chan struct{})}
	b.subs[c] = make(map[string]float64, 4)

	b.handleSubscribe(c, []string{"BTC-USDT", "", ":BTC", "binance:BTC"})

	if got := len(b.subs[c]); got != 1 {
		t.Errorf("only 'binance:BTC' should be accepted; got %d entries: %v", got, b.subs[c])
	}
}

// helper used in cap test — small impl avoids fmt dependency
func intToStr(n int) string {
	if n == 0 {
		return "0"
	}
	var buf [16]byte
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	return string(buf[i:])
}
