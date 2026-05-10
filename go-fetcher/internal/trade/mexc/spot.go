// MEXC SPOT extension — different host (api.mexc.com vs the futures
// contract.mexc.com), different signing scheme (Binance-clone HMAC
// hex over query string + signature param vs MEXC futures'
// custom ApiKey/Request-Time/Signature headers).
//
// We give spot its own persistent http.Client (separate TCP pool to
// the spot host) initialised lazily on first call. DNS prewarm for
// api.mexc.com is added in trade/prewarm.go.
//
// Implements trade.SpotAdapter.

package mexc

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

const spotBaseURL = "https://api.mexc.com"

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

func (a *Adapter) signedSpotRequest(
	ctx context.Context, creds trade.Creds, method, path string,
	params map[string]string,
) (json.RawMessage, error) {
	if params == nil {
		params = map[string]string{}
	}
	params["timestamp"] = strconv.FormatInt(time.Now().UnixMilli(), 10)
	if _, ok := params["recvWindow"]; !ok {
		params["recvWindow"] = "5000"
	}
	q := trade.SortedFormQuery(params)
	sig := trade.HMACHexSHA256(creds.APISecret, q)
	full := q + "&signature=" + sig

	var req *http.Request
	var err error
	switch method {
	case http.MethodGet, http.MethodDelete:
		req, err = http.NewRequestWithContext(ctx, method, spotBaseURL+path+"?"+full, nil)
	case http.MethodPost:
		req, err = http.NewRequestWithContext(ctx, method, spotBaseURL+path, strings.NewReader(full))
		if req != nil {
			req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
		}
	default:
		return nil, errUser("unsupported method %s", method)
	}
	if err != nil {
		return nil, err
	}
	req.Header.Set("X-MEXC-APIKEY", creds.APIKey)
	resp, err := getSpotClient().Do(req)
	if err != nil {
		return nil, &trade.Error{Kind: trade.KindTransient, Message: err.Error(), Cause: err}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		var e struct {
			Code int    `json:"code"`
			Msg  string `json:"msg"`
		}
		_ = json.Unmarshal(raw, &e)
		return nil, &trade.Error{
			Kind:    trade.KindExchange,
			Code:    strconv.Itoa(e.Code),
			Message: e.Msg,
		}
	}
	return raw, nil
}

func toMexcSpot(sym string) string {
	return strings.ToUpper(strings.TrimSpace(sym)) + "USDT"
}

func (a *Adapter) PlaceSpotOrder(ctx context.Context, creds trade.Creds, req trade.OpenRequest) (*trade.Result, error) {
	if err := req.Validate(); err != nil {
		return nil, err
	}
	side := "BUY"
	if req.Side == trade.SideSell {
		side = "SELL"
	}
	body, err := a.signedSpotRequest(ctx, creds, http.MethodPost, "/api/v3/order",
		map[string]string{
			"symbol":   toMexcSpot(req.Symbol),
			"side":     side,
			"type":     "MARKET",
			"quantity": strconv.FormatFloat(req.Quantity, 'f', -1, 64),
		})
	if err != nil {
		return nil, err
	}
	var resp struct {
		OrderID  json.Number `json:"orderId"`
		ClientID string      `json:"clientOrderId"`
		Status   string      `json:"status"`
	}
	_ = json.Unmarshal(body, &resp)
	return &trade.Result{
		OrderID:       string(resp.OrderID),
		Symbol:        req.Symbol,
		Side:          req.Side,
		Quantity:      req.Quantity,
		Status:        nonEmpty(resp.Status, "NEW"),
		ClientOrderID: resp.ClientID,
		CreatedAt:     time.Now().UTC(),
		Raw:           body,
	}, nil
}

func (a *Adapter) CloseSpotPosition(ctx context.Context, creds trade.Creds, req trade.CloseRequest) (*trade.Result, error) {
	base := strings.ToUpper(strings.TrimSpace(req.Symbol))
	if base == "" {
		return nil, errUser("symbol required")
	}
	body, err := a.signedSpotRequest(ctx, creds, http.MethodGet, "/api/v3/account", nil)
	if err != nil {
		return nil, err
	}
	var acct struct {
		Balances []struct {
			Asset string `json:"asset"`
			Free  string `json:"free"`
		} `json:"balances"`
	}
	_ = json.Unmarshal(body, &acct)
	freeBase := 0.0
	for _, b := range acct.Balances {
		if b.Asset == base {
			freeBase, _ = strconv.ParseFloat(b.Free, 64)
			break
		}
	}
	if freeBase <= 0 {
		return nil, errUser("No %s balance to close on MEXC spot", base)
	}
	out, err := a.signedSpotRequest(ctx, creds, http.MethodPost, "/api/v3/order",
		map[string]string{
			"symbol":   base + "USDT",
			"side":     "SELL",
			"type":     "MARKET",
			"quantity": strconv.FormatFloat(freeBase, 'f', -1, 64),
		})
	if err != nil {
		return nil, err
	}
	var resp struct {
		OrderID json.Number `json:"orderId"`
	}
	_ = json.Unmarshal(out, &resp)
	return &trade.Result{
		OrderID:   string(resp.OrderID),
		Symbol:    req.Symbol,
		Side:      trade.SideSell,
		Quantity:  freeBase,
		Status:    "NEW",
		CreatedAt: time.Now().UTC(),
		Raw:       out,
	}, nil
}

func nonEmpty(a, b string) string {
	if a != "" {
		return a
	}
	return b
}
