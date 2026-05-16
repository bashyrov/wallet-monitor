// Hyperliquid SPOT extension to the perp adapter.
//
// Reuses the same phantom-agent EIP-712 signing — only the asset-index
// encoding differs (spot adds 10000 to the universe index) and the close
// path queries spotClearinghouseState instead of clearinghouseState.
//
// Spot universe (from /info type:spotMeta):
//
//	{
//	  "universe": [{ "name":"HYPE/USDC", "index":107, "tokens":[150,0] }, ...],
//	  "tokens":   [{ "name":"USDC", "index":0 }, { "name":"HYPE", "index":150 }, ...]
//	}
//
// For trading, asset_id = universe.index + 10000  (HL convention).
// Quote is always USDC on spot.
//
// Quirks:
//   - PURR/USDC is the only canonical-name pair; everything else's
//     `name` field is "@N" and the base symbol must be resolved via
//     tokens[universe.tokens[0]].name.
//   - Balance lookup: POST /info {"type":"spotClearinghouseState",
//     "user":"<l1 addr>"} returns {"balances":[{"coin","total","hold"}]}.
//   - Spot has no leverage / margin / hedge mode — single signed POST
//     places the order.
//
// Implements trade.SpotAdapter.
package hyperliquid

import (
	"bytes"
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

const (
	spotMetaTTL = 30 * time.Minute
	spotAssetOffset = 10000 // HL spec: spot asset_id = universe.index + 10000
)

type spotPair struct {
	UniverseIndex int    // raw .index from /info spotMeta universe array
	BaseSymbol    string // resolved via tokens lookup ("HYPE" etc.)
	QuoteSymbol   string // typically "USDC"
}

var (
	spotMetaMu          sync.RWMutex
	spotMetaBySymbol    map[string]spotPair // "HYPE" -> {Index, "HYPE", "USDC"}
	spotMetaLastRefresh time.Time
)

func (a *Adapter) refreshSpotMeta(ctx context.Context) error {
	spotMetaMu.RLock()
	if spotMetaBySymbol != nil && time.Since(spotMetaLastRefresh) < spotMetaTTL {
		spotMetaMu.RUnlock()
		return nil
	}
	spotMetaMu.RUnlock()

	body := []byte(`{"type":"spotMeta"}`)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, baseURL+"/info", bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return &trade.Error{Kind: trade.KindTransient, Message: err.Error(), Cause: err}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return errInternal("spotMeta fetch failed", nil)
	}
	var doc struct {
		Universe []struct {
			Name   string `json:"name"`
			Index  int    `json:"index"`
			Tokens [2]int `json:"tokens"`
		} `json:"universe"`
		Tokens []struct {
			Name  string `json:"name"`
			Index int    `json:"index"`
		} `json:"tokens"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		return errInternal("parse spotMeta", err)
	}
	tokenName := make(map[int]string, len(doc.Tokens))
	for _, t := range doc.Tokens {
		tokenName[t.Index] = t.Name
	}
	out := make(map[string]spotPair, len(doc.Universe))
	for _, u := range doc.Universe {
		base := tokenName[u.Tokens[0]]
		quote := tokenName[u.Tokens[1]]
		if base == "" || quote == "" {
			continue
		}
		// Only USDC-quoted pairs are tradeable as arb-spot legs.
		if quote != "USDC" {
			continue
		}
		out[strings.ToUpper(base)] = spotPair{
			UniverseIndex: u.Index,
			BaseSymbol:    base,
			QuoteSymbol:   quote,
		}
	}
	spotMetaMu.Lock()
	spotMetaBySymbol = out
	spotMetaLastRefresh = time.Now()
	spotMetaMu.Unlock()
	return nil
}

func (a *Adapter) spotPairFor(ctx context.Context, sym string) (spotPair, error) {
	if err := a.refreshSpotMeta(ctx); err != nil {
		return spotPair{}, err
	}
	spotMetaMu.RLock()
	p, ok := spotMetaBySymbol[strings.ToUpper(sym)]
	spotMetaMu.RUnlock()
	if !ok {
		return spotPair{}, errUser("HL spot: pair %s/USDC not listed", strings.ToUpper(sym))
	}
	return p, nil
}

// PlaceSpotOrder — same orderAction shape as perp, but asset id = idx+10000.
// Market orders are IoC at px="0" (HL handles slippage internally), same
// trick the perp adapter uses.
func (a *Adapter) PlaceSpotOrder(ctx context.Context, creds trade.Creds, req trade.OpenRequest) (*trade.Result, error) {
	if err := req.Validate(); err != nil {
		return nil, err
	}
	pair, err := a.spotPairFor(ctx, req.Symbol)
	if err != nil {
		return nil, err
	}
	px := "0"
	tif := "Ioc"
	if req.OrderType.IsLimit() {
		px = strconv.FormatFloat(req.LimitPrice, 'f', -1, 64)
		tif = "Gtc"
	}
	action := orderAction{
		Type: "order",
		Orders: []orderLeg{{
			A: pair.UniverseIndex + spotAssetOffset,
			B: req.Side == trade.SideBuy,
			P: px,
			S: qtyString(req.Quantity),
			R: false,
			T: orderTypeBox{Limit: orderLimit{Tif: tif}},
		}},
		Grouping: "na",
	}
	body, err := a.postAction(ctx, creds, action)
	if err != nil {
		return nil, err
	}
	oid, avgPx := extractOrderResult(body)
	return &trade.Result{
		OrderID:   oid,
		Symbol:    req.Symbol,
		Side:      req.Side,
		Quantity:  req.Quantity,
		AvgPrice:  avgPx,
		Status:    "filled",
		CreatedAt: time.Now().UTC(),
		Raw:       body,
	}, nil
}

// CloseSpotPosition — sell entire base-asset balance. Fetched from
// spotClearinghouseState which lists every coin the user holds on the
// L1 spot wallet (separate from perp margin balance).
func (a *Adapter) CloseSpotPosition(ctx context.Context, creds trade.Creds, req trade.CloseRequest) (*trade.Result, error) {
	base := strings.ToUpper(strings.TrimSpace(req.Symbol))
	if base == "" {
		return nil, errUser("symbol required")
	}
	// l1 address comes from creds.APIKey per HL convention (creds.APIKey
	// = main wallet addr, creds.APISecret = agent private key hex).
	addr := strings.ToLower(strings.TrimSpace(creds.APIKey))
	if addr == "" {
		return nil, errUser("missing l1 wallet address (APIKey)")
	}
	queryBody := []byte(`{"type":"spotClearinghouseState","user":"` + addr + `"}`)
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, baseURL+"/info", bytes.NewReader(queryBody))
	if err != nil {
		return nil, err
	}
	httpReq.Header.Set("Content-Type", "application/json")
	resp, err := a.httpClient.Do(httpReq)
	if err != nil {
		return nil, &trade.Error{Kind: trade.KindTransient, Message: err.Error(), Cause: err}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return nil, &trade.Error{Kind: trade.KindExchange, Message: "spotClearinghouseState failed: " + string(raw)}
	}
	var doc struct {
		Balances []struct {
			Coin  string `json:"coin"`
			Total string `json:"total"`
			Hold  string `json:"hold"`
		} `json:"balances"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		return nil, errInternal("parse spot balance", err)
	}
	var freeBase float64
	for _, b := range doc.Balances {
		if strings.EqualFold(b.Coin, base) {
			total, _ := strconv.ParseFloat(b.Total, 64)
			hold, _ := strconv.ParseFloat(b.Hold, 64)
			freeBase = total - hold
			break
		}
	}
	if freeBase <= 0 {
		return nil, errUser("No %s spot balance to close on Hyperliquid", base)
	}
	pair, err := a.spotPairFor(ctx, base)
	if err != nil {
		return nil, err
	}
	action := orderAction{
		Type: "order",
		Orders: []orderLeg{{
			A: pair.UniverseIndex + spotAssetOffset,
			B: false, // sell
			P: "0",
			S: qtyString(freeBase),
			R: false,
			T: orderTypeBox{Limit: orderLimit{Tif: "Ioc"}},
		}},
		Grouping: "na",
	}
	body, err := a.postAction(ctx, creds, action)
	if err != nil {
		return nil, err
	}
	oid, avgPx := extractOrderResult(body)
	return &trade.Result{
		OrderID:   oid,
		Symbol:    req.Symbol,
		Side:      trade.SideSell,
		Quantity:  freeBase,
		AvgPrice:  avgPx,
		Status:    "filled",
		CreatedAt: time.Now().UTC(),
		Raw:       body,
	}, nil
}
