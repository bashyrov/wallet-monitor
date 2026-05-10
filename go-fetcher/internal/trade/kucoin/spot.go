// KuCoin SPOT extension — different host (api.kucoin.com vs futures'
// api-futures.kucoin.com), same signing scheme (HMAC-Base64-SHA256
// with passphrase), same KC-API-KEY/SIGN/TIMESTAMP/PASSPHRASE
// headers. Symbol form is BTC-USDT (dash, vs futures' XBTUSDTM).
//
// Implements trade.SpotAdapter.

package kucoin

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

const spotBaseURL = "https://api.kucoin.com"

var (
	spotClientOnce sync.Once
	spotClient     *http.Client
)

func getSpotClient() *http.Client {
	spotClientOnce.Do(func() {
		spotClient = &http.Client{
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
	return spotClient
}

func newSpotClientOID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return "av-" + hex.EncodeToString(b)
}

func (a *Adapter) signedSpotRequest(
	ctx context.Context, creds trade.Creds, method, path string,
	params map[string]string, body any,
) (json.RawMessage, error) {
	if creds.Passphrase == "" {
		return nil, errUser("KuCoin requires passphrase credential")
	}
	ts := strconv.FormatInt(time.Now().UnixMilli(), 10)

	urlPath := path
	bodyStr := ""
	if method == http.MethodGet && len(params) > 0 {
		urlPath = path + "?" + trade.SortedFormQuery(params)
	} else if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return nil, errInternal("marshal body", err)
		}
		bodyStr = string(b)
	}

	sig := trade.HMACBase64SHA256(creds.APISecret, ts+method+urlPath+bodyStr)
	passSig := trade.HMACBase64SHA256(creds.APISecret, creds.Passphrase)

	url := spotBaseURL + urlPath
	if method != http.MethodGet {
		url = spotBaseURL + path
	}
	var bodyReader io.Reader
	if method != http.MethodGet {
		if bodyStr == "" {
			bodyStr = "{}"
		}
		bodyReader = strings.NewReader(bodyStr)
	}
	req, err := http.NewRequestWithContext(ctx, method, url, bodyReader)
	if err != nil {
		return nil, err
	}
	req.Header.Set("KC-API-KEY", creds.APIKey)
	req.Header.Set("KC-API-SIGN", sig)
	req.Header.Set("KC-API-TIMESTAMP", ts)
	req.Header.Set("KC-API-PASSPHRASE", passSig)
	req.Header.Set("KC-API-KEY-VERSION", "2")
	req.Header.Set("Content-Type", "application/json")

	resp, err := getSpotClient().Do(req)
	if err != nil {
		return nil, &trade.Error{Kind: trade.KindTransient, Message: err.Error(), Cause: err}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return nil, parseError(resp.StatusCode, raw)
	}
	var env struct {
		Code string          `json:"code"`
		Msg  string          `json:"msg"`
		Data json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(raw, &env); err != nil {
		return nil, errInternal("parse spot envelope", err)
	}
	if env.Code != "200000" {
		return nil, &trade.Error{
			Kind:    trade.KindExchange,
			Code:    env.Code,
			Message: env.Msg,
		}
	}
	return env.Data, nil
}

func toKucoinSpot(sym string) string {
	return strings.ToUpper(strings.TrimSpace(sym)) + "-USDT"
}

func (a *Adapter) PlaceSpotOrder(ctx context.Context, creds trade.Creds, req trade.OpenRequest) (*trade.Result, error) {
	if err := req.Validate(); err != nil {
		return nil, err
	}
	side := "buy"
	if req.Side == trade.SideSell {
		side = "sell"
	}
	clientOID := newSpotClientOID()
	body, err := a.signedSpotRequest(ctx, creds, http.MethodPost, "/api/v1/orders", nil,
		map[string]any{
			"clientOid": clientOID,
			"symbol":    toKucoinSpot(req.Symbol),
			"side":      side,
			"type":      "market",
			"size":      strconv.FormatFloat(req.Quantity, 'f', -1, 64),
		})
	if err != nil {
		return nil, err
	}
	var resp struct {
		OrderID string `json:"orderId"`
	}
	_ = json.Unmarshal(body, &resp)
	return &trade.Result{
		OrderID:       resp.OrderID,
		Symbol:        req.Symbol,
		Side:          req.Side,
		Quantity:      req.Quantity,
		Status:        "NEW",
		ClientOrderID: clientOID,
		CreatedAt:     time.Now().UTC(),
		Raw:           body,
	}, nil
}

func (a *Adapter) CloseSpotPosition(ctx context.Context, creds trade.Creds, req trade.CloseRequest) (*trade.Result, error) {
	base := strings.ToUpper(strings.TrimSpace(req.Symbol))
	if base == "" {
		return nil, errUser("symbol required")
	}
	body, err := a.signedSpotRequest(ctx, creds, http.MethodGet, "/api/v1/accounts",
		map[string]string{"currency": base, "type": "trade"}, nil)
	if err != nil {
		return nil, err
	}
	var rows []struct {
		Currency  string `json:"currency"`
		Type      string `json:"type"`
		Available string `json:"available"`
	}
	_ = json.Unmarshal(body, &rows)
	freeBase := 0.0
	for _, r := range rows {
		if r.Currency == base && r.Type == "trade" {
			freeBase, _ = strconv.ParseFloat(r.Available, 64)
			break
		}
	}
	if freeBase <= 0 {
		return nil, errUser("No %s balance to close on KuCoin spot", base)
	}
	clientOID := newSpotClientOID()
	out, err := a.signedSpotRequest(ctx, creds, http.MethodPost, "/api/v1/orders", nil,
		map[string]any{
			"clientOid": clientOID,
			"symbol":    base + "-USDT",
			"side":      "sell",
			"type":      "market",
			"size":      strconv.FormatFloat(freeBase, 'f', -1, 64),
		})
	if err != nil {
		return nil, err
	}
	var resp struct {
		OrderID string `json:"orderId"`
	}
	_ = json.Unmarshal(out, &resp)
	return &trade.Result{
		OrderID:       resp.OrderID,
		Symbol:        req.Symbol,
		Side:          trade.SideSell,
		Quantity:      freeBase,
		Status:        "NEW",
		ClientOrderID: clientOID,
		CreatedAt:     time.Now().UTC(),
		Raw:           out,
	}, nil
}
