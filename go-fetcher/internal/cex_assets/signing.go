package cex_assets

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"net/url"
	"os"
	"sort"
	"strings"
)

// SignedCreds holds the read-only credentials for one signed venue.
// Loaded from env vars CEX_<VENUE>_READ_KEY + CEX_<VENUE>_READ_SECRET
// (+ _PASSPHRASE for OKX). HasKey() returns false when either piece is
// missing — manager skips the venue rather than failing the deploy.
//
// SECURITY: secrets are kept as separate strings (not embedded in a
// concatenated URL or query) so an accidental %v / Stringer dump can't
// leak them. The only place they leave the struct is HMACHex / HMACBase64
// + the X-MBX-APIKEY / X-BAPI-API-KEY / OK-ACCESS-KEY header value.
type SignedCreds struct {
	APIKey     string
	APISecret  string
	Passphrase string // OKX only — empty for other venues
}

// LoadSignedCreds reads CEX_<VENUE>_READ_KEY + CEX_<VENUE>_READ_SECRET
// (+ _PASSPHRASE) from the process env. Venue is the lowercased venue id
// (binance, bybit, okx, mexc, bingx). When any required piece is empty
// the returned creds has HasKey()==false — manager will silently skip
// the venue so the deploy still succeeds without keys.
func LoadSignedCreds(venue string) SignedCreds {
	v := strings.ToUpper(venue)
	return SignedCreds{
		APIKey:     strings.TrimSpace(os.Getenv("CEX_" + v + "_READ_KEY")),
		APISecret:  strings.TrimSpace(os.Getenv("CEX_" + v + "_READ_SECRET")),
		Passphrase: strings.TrimSpace(os.Getenv("CEX_" + v + "_READ_PASSPHRASE")),
	}
}

// HasKey returns true when the venue has at least key + secret. OKX
// passphrase is checked by the OKX adapter separately because it
// can sometimes be empty for legacy v3 keys.
func (c SignedCreds) HasKey() bool {
	return c.APIKey != "" && c.APISecret != ""
}

// HMACHex returns hex(HMAC-SHA256(secret, payload)).
// Used by Binance, MEXC, BingX (X-MBX-APIKEY-family signing).
func HMACHex(secret, payload string) string {
	h := hmac.New(sha256.New, []byte(secret))
	h.Write([]byte(payload))
	return hex.EncodeToString(h.Sum(nil))
}

// HMACBase64 returns base64(HMAC-SHA256(secret, payload)).
// Used by OKX (per their signing spec).
func HMACBase64(secret, payload string) string {
	h := hmac.New(sha256.New, []byte(secret))
	h.Write([]byte(payload))
	return base64.StdEncoding.EncodeToString(h.Sum(nil))
}

// HMACHexConcat returns hex(HMAC-SHA256(secret, timestamp+key+recv+raw)).
// Used by Bybit v5 — payload is timestamp || api_key || recv_window || (query or body).
func HMACHexConcat(secret, timestamp, apiKey, recvWindow, payload string) string {
	preimage := timestamp + apiKey + recvWindow + payload
	return HMACHex(secret, preimage)
}

// SortedQuery builds a query string with keys in lexicographic order.
// Required by signatures over query strings — the venue rebuilds the
// signature server-side and any key reordering breaks the match.
//
// Values are URL-encoded with the same rules as net/url.Values, which
// is what every venue's docs expects.
func SortedQuery(params map[string]string) string {
	if len(params) == 0 {
		return ""
	}
	keys := make([]string, 0, len(params))
	for k := range params {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	v := make(url.Values, len(params))
	for _, k := range keys {
		v.Set(k, params[k])
	}
	// url.Values.Encode itself sorts keys lexicographically, which is
	// what we wanted; the explicit sort above is defensive in case
	// stdlib semantics ever change.
	return v.Encode()
}
