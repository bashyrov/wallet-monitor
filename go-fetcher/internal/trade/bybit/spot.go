// Bybit V5 SPOT extension — same baseURL, same signed envelope, same
// endpoint (/v5/order/create) — only `category` flips from "linear" to
// "spot" and we skip leverage/margin entirely.
//
// Implements trade.SpotAdapter on the existing futures Adapter.

package bybit

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

// spotInstrumentInfo — separate cache for spot category. Bybit's
// /v5/market/instruments-info returns different lot filters per
// category; reuse of the futures cache would write the wrong values.
type spotSymbolInfo struct {
	BasePrecision    string
	QuotePrecision   string
	MinOrderQty      float64
	MinNotional      float64
	BasePrecisionStr string
}

func (a *Adapter) spotInstrument(ctx context.Context, sym string) (spotSymbolInfo, error) {
	url := baseURL + "/v5/market/instruments-info?category=spot&symbol=" + sym
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return spotSymbolInfo{}, &trade.Error{Kind: trade.KindTransient, Message: err.Error()}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var env struct {
		Result struct {
			List []struct {
				Symbol     string `json:"symbol"`
				LotSizeFilter struct {
					BasePrecision    string `json:"basePrecision"`
					QuotePrecision   string `json:"quotePrecision"`
					MinOrderQty      string `json:"minOrderQty"`
					MinOrderAmt      string `json:"minOrderAmt"`
				} `json:"lotSizeFilter"`
			} `json:"list"`
		} `json:"result"`
	}
	if err := json.Unmarshal(raw, &env); err != nil {
		return spotSymbolInfo{}, errInternal("parse spot instruments-info", err)
	}
	if len(env.Result.List) == 0 {
		return spotSymbolInfo{}, &trade.Error{
			Kind:    trade.KindUser,
			Message: fmt.Sprintf("symbol %s not listed on Bybit spot", sym),
		}
	}
	it := env.Result.List[0]
	return spotSymbolInfo{
		BasePrecision:    it.LotSizeFilter.BasePrecision,
		QuotePrecision:   it.LotSizeFilter.QuotePrecision,
		MinOrderQty:      parseFloat(it.LotSizeFilter.MinOrderQty),
		MinNotional:      parseFloat(it.LotSizeFilter.MinOrderAmt),
		BasePrecisionStr: it.LotSizeFilter.BasePrecision,
	}, nil
}

func (a *Adapter) PlaceSpotOrder(ctx context.Context, creds trade.Creds, req trade.OpenRequest) (*trade.Result, error) {
	if err := req.Validate(); err != nil {
		return nil, err
	}
	sym := toBybit(req.Symbol)
	info, err := a.spotInstrument(ctx, sym)
	if err != nil {
		return nil, err
	}
	side := "Buy"
	if req.Side == trade.SideSell {
		side = "Sell"
	}
	if info.MinOrderQty > 0 && req.Quantity < info.MinOrderQty {
		return nil, errUser("Quantity below minimum (%g %s)", info.MinOrderQty, req.Symbol)
	}
	// Bybit spot market orders default `qty` to QUOTE currency for BUY
	// and BASE currency for SELL. We always pass base-coin quantity, so
	// force marketUnit=baseCoin to keep semantics consistent.
	body, err := a.signedRequest(ctx, creds, http.MethodPost,
		"/v5/order/create", nil, map[string]any{
			"category":   "spot",
			"symbol":     sym,
			"side":       side,
			"orderType":  "Market",
			"qty":        qtyString(req.Quantity),
			"marketUnit": "baseCoin",
		})
	if err != nil {
		return nil, err
	}
	var resp struct {
		OrderID  string `json:"orderId"`
		ClientID string `json:"orderLinkId"`
	}
	_ = json.Unmarshal(body, &resp)
	return &trade.Result{
		OrderID:       resp.OrderID,
		Symbol:        req.Symbol,
		Side:          req.Side,
		Quantity:      req.Quantity,
		Status:        "NEW",
		ClientOrderID: resp.ClientID,
		CreatedAt:     time.Now().UTC(),
		Raw:           body,
	}, nil
}

// CloseSpotPosition — for spot, sell the entire base-asset balance.
// Bybit's /v5/account/wallet-balance accountType=UNIFIED returns coin
// balances; we sell free balance of `req.Symbol` for USDT.
func (a *Adapter) CloseSpotPosition(ctx context.Context, creds trade.Creds, req trade.CloseRequest) (*trade.Result, error) {
	base := strings.ToUpper(strings.TrimSpace(req.Symbol))
	if base == "" {
		return nil, errUser("symbol required")
	}
	body, err := a.signedRequest(ctx, creds, http.MethodGet,
		"/v5/account/wallet-balance",
		map[string]string{"accountType": "UNIFIED", "coin": base},
		nil)
	if err != nil {
		return nil, err
	}
	// signedRequest already unwraps Bybit's outer envelope — body is the
	// `result` content directly. Walk list[0].coin[] for the base asset.
	var env struct {
		List []struct {
			Coin []struct {
				Coin                string `json:"coin"`
				AvailableToWithdraw string `json:"availableToWithdraw"`
				WalletBalance       string `json:"walletBalance"`
				Free                string `json:"free"`
			} `json:"coin"`
		} `json:"list"`
	}
	if err := json.Unmarshal(body, &env); err != nil {
		return nil, errInternal("parse wallet-balance", err)
	}
	var freeBase float64
	if len(env.List) > 0 {
		for _, c := range env.List[0].Coin {
			if c.Coin == base {
				freeBase = parseFloat(c.Free)
				if freeBase == 0 {
					freeBase = parseFloat(c.AvailableToWithdraw)
				}
				if freeBase == 0 {
					freeBase = parseFloat(c.WalletBalance)
				}
				break
			}
		}
	}
	if freeBase <= 0 {
		return nil, errUser("No %s balance to close on Bybit spot", base)
	}
	// Round DOWN to the symbol's basePrecision — Bybit rejects with
	// "Order quantity has too many decimals" if the qty exceeds the lot.
	sym := base + "USDT"
	qtyStr := qtyString(freeBase)
	if info, ierr := a.spotInstrument(ctx, sym); ierr == nil && info.BasePrecisionStr != "" {
		if step, perr := strconv.ParseFloat(info.BasePrecisionStr, 64); perr == nil && step > 0 {
			rounded := float64(int64(freeBase/step)) * step
			// Match the precision's decimal count for clean serialization.
			prec := 0
			if i := strings.IndexByte(info.BasePrecisionStr, '.'); i >= 0 {
				prec = len(info.BasePrecisionStr) - i - 1
			}
			qtyStr = strconv.FormatFloat(rounded, 'f', prec, 64)
		}
	}
	out, err := a.signedRequest(ctx, creds, http.MethodPost,
		"/v5/order/create", nil, map[string]any{
			"category":   "spot",
			"symbol":     sym,
			"side":       "Sell",
			"orderType":  "Market",
			"qty":        qtyStr,
			"marketUnit": "baseCoin",
		})
	if err != nil {
		return nil, err
	}
	var resp struct {
		OrderID string `json:"orderId"`
	}
	_ = json.Unmarshal(out, &resp)
	return &trade.Result{
		OrderID:   resp.OrderID,
		Symbol:    req.Symbol,
		Side:      trade.SideSell,
		Quantity:  freeBase,
		Status:    "NEW",
		CreatedAt: time.Now().UTC(),
		Raw:       out,
	}, nil
}
