package trade

import (
	"crypto/hmac"
	"crypto/sha256"
	"crypto/sha512"
	"encoding/base64"
	"encoding/hex"
	"hash"
	"net/url"
	"sort"
	"strings"
)

// HMACHexSHA256 — the most common signing flavour (Binance, Bybit,
// MEXC, BingX, Aster). Returns the lowercase hex digest that goes in
// the `signature=` query param or `X-MBX-SIGNATURE`-style header.
func HMACHexSHA256(secret, payload string) string {
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(payload))
	return hex.EncodeToString(mac.Sum(nil))
}

// HMACBase64SHA256 — OKX / KuCoin flavour. Returns base64(HMAC-SHA256).
func HMACBase64SHA256(secret, payload string) string {
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(payload))
	return base64.StdEncoding.EncodeToString(mac.Sum(nil))
}

// HMACBase64SHA512 — Kraken flavour. Used in the Kraken-futures /
// Bitfinex space.
func HMACBase64SHA512(secret, payload string) string {
	mac := hmac.New(sha512.New, []byte(secret))
	mac.Write([]byte(payload))
	return base64.StdEncoding.EncodeToString(mac.Sum(nil))
}

// HMACWith — escape hatch for unusual flavours (Bitget uses
// hex-of-base64-of-secret first, etc.).
func HMACWith(h func() hash.Hash, secret, payload string) []byte {
	mac := hmac.New(h, []byte(secret))
	mac.Write([]byte(payload))
	return mac.Sum(nil)
}

// SortedFormQuery — deterministic query string for signing. Mirrors
// Python's `urllib.parse.urlencode(sorted(params.items()))`. Skips
// empty values like Binance does.
func SortedFormQuery(params map[string]string) string {
	keys := make([]string, 0, len(params))
	for k, v := range params {
		if v == "" {
			continue
		}
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, url.QueryEscape(k)+"="+url.QueryEscape(params[k]))
	}
	return strings.Join(parts, "&")
}
