// HTX SPOT extension — different host (api.huobi.pro vs futures' api.hbdm.com),
// same multi-line HMAC-SHA256-base64 signing scheme. Symbol form: "btcusdt"
// (lowercase, no dash). Quantity in coins, not contracts.
//
// Endpoints used:
//   GET  /v1/account/accounts          — find spot account ID
//   GET  /v1/account/accounts/{id}/balance — spot balances
//   POST /v1/order/orders/place        — place order
//
// Implements trade.SpotAdapter.
package htx

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

const (
	spotBase = "https://api.huobi.pro"
	spotHost = "api.huobi.pro"
)

var (
	spotClientOnce sync.Once
	spotHTTPClient *http.Client
)

func getSpotClient() *http.Client {
	spotClientOnce.Do(func() {
		spotHTTPClient = &http.Client{
			Timeout: 15 * time.Second,
			Transport: &http.Transport{
				ForceAttemptHTTP2:   true,
				MaxIdleConns:        200,
				MaxIdleConnsPerHost: 32,
				MaxConnsPerHost:     64,
				IdleConnTimeout:     300 * time.Second,
				TLSHandshakeTimeout: 5 * time.Second,
			},
		}
	})
	return spotHTTPClient
}

// spotAccountCache — per-APIKey spot account ID cache (account IDs don't change).
var (
	spotAcctMu    sync.RWMutex
	spotAcctCache = make(map[string]int64)
)

func toHtxSpot(sym string) string {
	return strings.ToLower(strings.TrimSpace(sym)) + "usdt"
}

func (a *Adapter) signedSpotRequest(
	ctx context.Context, creds trade.Creds, method, path string,
	queryExtra map[string]string, body any,
) (json.RawMessage, error) {
	params := map[string]string{
		"AccessKeyId":      creds.APIKey,
		"SignatureMethod":  "HmacSHA256",
		"SignatureVersion": "2",
		"Timestamp":        htxTimestamp(),
	}
	for k, v := range queryExtra {
		params[k] = v
	}
	pre := signPayload(method, spotHost, path, params)
	params["Signature"] = trade.HMACBase64SHA256(creds.APISecret, pre)

	keys := make([]string, 0, len(params))
	for k := range params {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, url.QueryEscape(k)+"="+url.QueryEscape(params[k]))
	}
	u := spotBase + path + "?" + strings.Join(parts, "&")

	var bodyReader io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return nil, errInternal("marshal body", err)
		}
		bodyReader = strings.NewReader(string(b))
	}
	req, err := http.NewRequestWithContext(ctx, method, u, bodyReader)
	if err != nil {
		return nil, err
	}
	if method != http.MethodGet {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := getSpotClient().Do(req)
	if err != nil {
		return nil, &trade.Error{Kind: trade.KindTransient, Message: err.Error(), Cause: err}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		var env struct {
			ErrMsg string `json:"err-msg"`
			ErrCd  string `json:"err-code"`
		}
		_ = json.Unmarshal(raw, &env)
		return nil, &trade.Error{Kind: trade.KindExchange, Code: env.ErrCd, Message: env.ErrMsg}
	}
	var env struct {
		Status string          `json:"status"`
		ErrMsg string          `json:"err-msg"`
		ErrCd  string          `json:"err-code"`
		Data   json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(raw, &env); err != nil {
		return nil, errInternal("parse spot envelope", err)
	}
	if env.Status == "error" {
		return nil, &trade.Error{Kind: trade.KindExchange, Code: env.ErrCd, Message: env.ErrMsg}
	}
	return env.Data, nil
}

func (a *Adapter) htxSpotAccountID(ctx context.Context, creds trade.Creds) (int64, error) {
	spotAcctMu.RLock()
	id, ok := spotAcctCache[creds.APIKey]
	spotAcctMu.RUnlock()
	if ok {
		return id, nil
	}
	data, err := a.signedSpotRequest(ctx, creds, http.MethodGet, "/v1/account/accounts", nil, nil)
	if err != nil {
		return 0, err
	}
	var accounts []struct {
		ID    int64  `json:"id"`
		Type  string `json:"type"`
		State string `json:"state"`
	}
	if err := json.Unmarshal(data, &accounts); err != nil {
		return 0, errInternal("parse accounts", err)
	}
	for _, ac := range accounts {
		if ac.Type == "spot" && ac.State == "working" {
			spotAcctMu.Lock()
			spotAcctCache[creds.APIKey] = ac.ID
			spotAcctMu.Unlock()
			return ac.ID, nil
		}
	}
	return 0, errUser("no active spot account on this HTX key")
}

func (a *Adapter) PlaceSpotOrder(ctx context.Context, creds trade.Creds, req trade.OpenRequest) (*trade.Result, error) {
	if err := req.Validate(); err != nil {
		return nil, err
	}
	acctID, err := a.htxSpotAccountID(ctx, creds)
	if err != nil {
		return nil, err
	}
	sym := toHtxSpot(req.Symbol)
	orderType := "buy-market"
	if req.Side == trade.SideSell {
		orderType = "sell-market"
	}
	body := map[string]any{
		"account-id": strconv.FormatInt(acctID, 10),
		"symbol":     sym,
		"type":       orderType,
		"amount":     strconv.FormatFloat(req.Quantity, 'f', -1, 64),
	}
	data, err := a.signedSpotRequest(ctx, creds, http.MethodPost, "/v1/order/orders/place", nil, body)
	if err != nil {
		return nil, err
	}
	// HTX spot returns bare order ID string as "data"
	orderID := ""
	var s string
	if json.Unmarshal(data, &s) == nil {
		orderID = s
	} else {
		var n json.Number
		_ = json.Unmarshal(data, &n)
		orderID = string(n)
	}
	return &trade.Result{
		OrderID:   orderID,
		Symbol:    req.Symbol,
		Side:      req.Side,
		Quantity:  req.Quantity,
		Status:    "NEW",
		CreatedAt: time.Now().UTC(),
		Raw:       data,
	}, nil
}

func (a *Adapter) CloseSpotPosition(ctx context.Context, creds trade.Creds, req trade.CloseRequest) (*trade.Result, error) {
	base := strings.ToUpper(strings.TrimSpace(req.Symbol))
	if base == "" {
		return nil, errUser("symbol required")
	}
	acctID, err := a.htxSpotAccountID(ctx, creds)
	if err != nil {
		return nil, err
	}
	// Fetch spot balances for this account
	path := "/v1/account/accounts/" + strconv.FormatInt(acctID, 10) + "/balance"
	data, err := a.signedSpotRequest(ctx, creds, http.MethodGet, path, nil, nil)
	if err != nil {
		return nil, err
	}
	var acctData struct {
		List []struct {
			Currency string `json:"currency"`
			Type     string `json:"type"`
			Balance  string `json:"balance"`
		} `json:"list"`
	}
	_ = json.Unmarshal(data, &acctData)
	freeBase := 0.0
	for _, row := range acctData.List {
		if strings.ToUpper(row.Currency) == base && row.Type == "trade" {
			freeBase, _ = strconv.ParseFloat(row.Balance, 64)
			break
		}
	}
	if freeBase <= 0 {
		return nil, errUser("no %s balance to close on HTX spot", base)
	}
	sym := strings.ToLower(base) + "usdt"
	body := map[string]any{
		"account-id": strconv.FormatInt(acctID, 10),
		"symbol":     sym,
		"type":       "sell-market",
		"amount":     strconv.FormatFloat(freeBase, 'f', -1, 64),
	}
	out, err := a.signedSpotRequest(ctx, creds, http.MethodPost, "/v1/order/orders/place", nil, body)
	if err != nil {
		return nil, err
	}
	orderID := ""
	var s string
	if json.Unmarshal(out, &s) == nil {
		orderID = s
	}
	return &trade.Result{
		OrderID:   orderID,
		Symbol:    req.Symbol,
		Side:      trade.SideSell,
		Quantity:  freeBase,
		Status:    "NEW",
		CreatedAt: time.Now().UTC(),
		Raw:       out,
	}, nil
}
