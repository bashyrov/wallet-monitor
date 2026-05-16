// Kraken SPOT (api.kraken.com /0/private) extension to the futures adapter.
//
// Signing flavour (different from futures!):
//
//	sha256 = SHA256(nonce + POST_data)
//	sig    = base64(HMAC_SHA512(base64_decode(api_secret), uri_path + sha256))
//
// Headers:
//
//	API-Key:  <api_key>
//	API-Sign: <base64 sig>
//
// POST body uses URL-encoded form with `nonce=<ms>` always required.
//
// Symbol form for spot uses pair codes ("XBTUSDT", "ETHUSDT") — same XBT-for-
// BTC mapping as futures. We pass `pair` (alternative names) param to be
// resilient against Kraken's mixed naming (XXBTZUSD vs XBTUSDT etc.).
//
// Quantity in BASE asset (no contract conversion). Spot only supports
// `ordertype=market` for our use-case (arb leg). No leverage / margin.
//
// Implements trade.SpotAdapter.

package kraken

import (
	"context"
	"crypto/sha256"
	"crypto/sha512"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

const (
	spotBaseURL = "https://api.kraken.com"
	spotRoot    = "/0/private"
)

// toKrakenSpotPair — Kraken spot uses XBTUSDT / ETHUSDT style. For BTC
// we map to XBT (Kraken's historical naming).
func toKrakenSpotPair(sym string) string {
	s := strings.ToUpper(sym)
	if s == "BTC" {
		s = "XBT"
	}
	return s + "USDT"
}

// ── Spot signing ─────────────────────────────────────────────────────────

func krakenSpotSign(apiSecret, uriPath, nonce, postData string) (string, error) {
	secret, err := base64.StdEncoding.DecodeString(apiSecret)
	if err != nil {
		return "", err
	}
	// SHA-256 of (nonce + postData)
	noncePost := nonce + postData
	hash := sha256.Sum256([]byte(noncePost))
	// HMAC-SHA512 of (uriPath || sha256_bytes) with decoded secret.
	msg := append([]byte(uriPath), hash[:]...)
	mac := trade.HMACWith(sha512.New, string(secret), string(msg))
	return base64.StdEncoding.EncodeToString(mac), nil
}

func (a *Adapter) signedSpotRequest(
	ctx context.Context, creds trade.Creds, path string,
	params map[string]string,
) (json.RawMessage, error) {
	if params == nil {
		params = map[string]string{}
	}
	nonce := strconv.FormatInt(time.Now().UnixMilli(), 10)
	params["nonce"] = nonce

	v := url.Values{}
	for k, val := range params {
		v.Set(k, val)
	}
	postData := v.Encode()

	uriPath := spotRoot + path
	sig, err := krakenSpotSign(creds.APISecret, uriPath, nonce, postData)
	if err != nil {
		return nil, errInternal("base64 spot secret decode", err)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, spotBaseURL+uriPath, strings.NewReader(postData))
	if err != nil {
		return nil, err
	}
	req.Header.Set("API-Key", creds.APIKey)
	req.Header.Set("API-Sign", sig)
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Accept", "application/json")

	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, &trade.Error{Kind: trade.KindTransient, Message: err.Error(), Cause: err}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return nil, parseError(resp.StatusCode, raw)
	}
	// Kraken spot wraps everything in `{"error":[...], "result":{...}}`.
	// Any non-empty error array = failure even on HTTP 200.
	var env struct {
		Error  []string        `json:"error"`
		Result json.RawMessage `json:"result"`
	}
	if jerr := json.Unmarshal(raw, &env); jerr != nil {
		return nil, errInternal("parse spot envelope", jerr)
	}
	if len(env.Error) > 0 {
		return nil, &trade.Error{Kind: trade.KindExchange, Message: strings.Join(env.Error, "; ")}
	}
	return env.Result, nil
}

// PlaceSpotOrder — POST /0/private/AddOrder, type=market.
// BUY uses `volume` in BASE (we follow the same convention as Binance spot,
// which lets the arb engine treat both venues uniformly). Spot has no
// leverage / margin / hedge mode — single signed POST does it all.
func (a *Adapter) PlaceSpotOrder(ctx context.Context, creds trade.Creds, req trade.OpenRequest) (*trade.Result, error) {
	if err := req.Validate(); err != nil {
		return nil, err
	}
	pair := toKrakenSpotPair(req.Symbol)
	side := "buy"
	if req.Side == trade.SideSell {
		side = "sell"
	}
	params := map[string]string{
		"pair":      pair,
		"type":      side,
		"ordertype": "market",
		"volume":    strconv.FormatFloat(req.Quantity, 'f', -1, 64),
	}
	body, err := a.signedSpotRequest(ctx, creds, "/AddOrder", params)
	if err != nil {
		return nil, err
	}
	// AddOrder response: {"descr":{"order":"buy 1.00 XBTUSDT @ market"},
	//                     "txid":["O3LRNT-..."]}
	var resp struct {
		Descr struct {
			Order string `json:"order"`
		} `json:"descr"`
		Txid []string `json:"txid"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		return nil, errInternal("parse AddOrder", err)
	}
	orderID := ""
	if len(resp.Txid) > 0 {
		orderID = resp.Txid[0]
	}
	// Market orders fill immediately; Kraken doesn't return fill price in
	// AddOrder response. Caller's WS user-stream / reconcile worker fills
	// in avg_price asynchronously. Same fast-path as OKX.
	return &trade.Result{
		OrderID:   orderID,
		Symbol:    req.Symbol,
		Side:      req.Side,
		Quantity:  req.Quantity,
		Status:    "filled",
		CreatedAt: time.Now().UTC(),
		Raw:       body,
	}, nil
}

// CloseSpotPosition — sell entire BASE balance. Spot has no "close" concept;
// for arb-leg unwinding we dump the wallet's free balance back to USDT.
func (a *Adapter) CloseSpotPosition(ctx context.Context, creds trade.Creds, req trade.CloseRequest) (*trade.Result, error) {
	base := strings.ToUpper(strings.TrimSpace(req.Symbol))
	if base == "" {
		return nil, errUser("symbol required")
	}
	// Fetch wallet balance.
	body, err := a.signedSpotRequest(ctx, creds, "/Balance", nil)
	if err != nil {
		return nil, err
	}
	// Kraken balance: {"XBT":"0.123","USDT":"500.00","XXBT":"0.123",...}.
	// Asset names sometimes carry the X/Z prefix (XXBT = XBT, ZUSD = USD).
	// Normalise by stripping a leading X if asset length >= 4.
	var rawMap map[string]string
	if err := json.Unmarshal(body, &rawMap); err != nil {
		return nil, errInternal("parse balance", err)
	}
	wanted := base
	if wanted == "BTC" {
		wanted = "XBT"
	}
	var freeBase float64
	for k, v := range rawMap {
		norm := strings.TrimPrefix(k, "X")
		if k == wanted || norm == wanted {
			freeBase = parseFloatStr(v)
			break
		}
	}
	if freeBase <= 0 {
		return nil, errUser("No %s balance to close on Kraken spot", base)
	}
	pair := toKrakenSpotPair(base)
	params := map[string]string{
		"pair":      pair,
		"type":      "sell",
		"ordertype": "market",
		"volume":    strconv.FormatFloat(freeBase, 'f', -1, 64),
	}
	out, err := a.signedSpotRequest(ctx, creds, "/AddOrder", params)
	if err != nil {
		return nil, err
	}
	var resp struct {
		Txid []string `json:"txid"`
	}
	if err := json.Unmarshal(out, &resp); err != nil {
		return nil, errInternal("parse close", err)
	}
	orderID := ""
	if len(resp.Txid) > 0 {
		orderID = resp.Txid[0]
	}
	return &trade.Result{
		OrderID:   orderID,
		Symbol:    req.Symbol,
		Side:      trade.SideSell,
		Quantity:  freeBase,
		Status:    "filled",
		CreatedAt: time.Now().UTC(),
		Raw:       out,
	}, nil
}

// parseFloatStr is a tiny helper; Kraken returns balance as raw strings.
func parseFloatStr(s string) float64 {
	f, _ := strconv.ParseFloat(strings.TrimSpace(s), 64)
	return f
}
