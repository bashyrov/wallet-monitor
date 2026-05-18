// Extended (StarkEx-based perp DEX) trade adapter.
//
// Reference: https://api.docs.extended.exchange/
// SDK source (canonical): https://github.com/x10xchange/python_sdk (`starknet` branch)
//
// Auth model:
//
//	GET   — X-Api-Key header only (no Stark sig)
//	POST  — X-Api-Key header + Stark signature inlined in body.settlement.signature
//	DELETE— X-Api-Key header only (cancellation is identified by orderId)
//
// Credential layout in our Creds struct:
//
//	APIKey      → "api_key" from Extended UI's API Management
//	APISecret   → Stark L2 private key (hex)
//	Wallet      → Stark L2 public key (hex)
//	Passphrase  → vault / collateral_position_id (decimal string)
//
// Order signing (matches x10's `fast_stark_crypto.get_order_msg_hash`):
//
//	PoseidonArray of 14 felts in order:
//	  1.  position_id (vault)
//	  2.  base_asset_id  (synthetic.settlement_external_id)
//	  3.  base_amount    (signed; negative when selling synthetic)
//	  4.  quote_asset_id (collateral.settlement_external_id)
//	  5.  quote_amount   (signed; negative when buying synthetic)
//	  6.  fee_amount
//	  7.  fee_asset_id   (= collateral asset id)
//	  8.  expiration_timestamp (unix seconds, includes +14 day buffer)
//	  9.  salt (nonce)
//	  10. user_public_key
//	  11. domain.name      → felt("Perpetuals")
//	  12. domain.version   → felt("v0")
//	  13. domain.chain_id  → felt("SN_MAIN")
//	  14. domain.revision  → 1
//
// CAVEAT: Signing has NO live cross-vector against the x10 SDK yet — same
// caveat as Paradex. First real order on Extended testnet is the truth check.
// Keep "extended" out of GO_TRADE_VENUES until verified.
package extended

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"math/big"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"crypto/sha256"
	"encoding/hex"

	"github.com/NethermindEth/juno/core/felt"
	"github.com/NethermindEth/starknet.go/curve"
	stark_utils "github.com/NethermindEth/starknet.go/utils"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

const (
	baseURL = "https://api.starknet.extended.exchange/api/v1"
	// Slippage cap for market orders. SDK default is 0.75% on the worst-case
	// quote price used for signing. We pad signed quote_amount by this.
	marketSlippage = 0.0075
	// Expiration window for an order; SDK adds +14 days to the signed
	// expiration_timestamp as anti-replay buffer.
	signBufferDays = 14
)

// Stark domain felts + selectors — encoded once at init.
//
// Reference (matched byte-for-byte against the x10 official Rust SDK at
// github.com/x10xchange/rust-crypto-lib-base, file src/starknet_messages.rs):
//
//   ORDER selector       — Poseidon-bucket tag for Order(...) struct
//   DOMAIN selector      — Poseidon-bucket tag for StarknetDomain(...) struct
//   MESSAGE_FELT         — cairo_short_string_to_felt("StarkNet Message")
//   domain               — {name:"Perpetuals", version:"v0", chain_id:"SN_MAIN", revision:1}
//
// Signing flow:
//   orderHash   = Poseidon(ORDER_SEL, position_id, base_id, base_amt, quote_id,
//                          quote_amt, fee_id, fee_amt, expiry, salt)
//   domainHash  = Poseidon(DOMAIN_SEL, name, version, chain_id, revision)
//   messageHash = Poseidon(MESSAGE_FELT, domainHash, pubkey, orderHash)
//   (r, s)      = StarkSign(messageHash, privKey)
var (
	domainName       *felt.Felt
	domainVersion    *felt.Felt
	domainChainID    *felt.Felt
	domainRevision   *felt.Felt
	orderSelector    *felt.Felt
	domainSelector   *felt.Felt
	starkNetMsgFelt  *felt.Felt
	domainHashCached *felt.Felt
)

func init() {
	mustFelt := func(s string) *felt.Felt {
		f, err := stark_utils.HexToFelt(s)
		if err != nil {
			panic("extended: domain felt: " + err.Error())
		}
		return f
	}
	domainName = encodeShortString("Perpetuals")
	domainVersion = encodeShortString("v0")
	domainChainID = encodeShortString("SN_MAIN")
	domainRevision = mustFelt("0x1")
	// Type-string selectors (SNIP-12 v1). Computed dynamically via
	// starknet_keccak rather than hardcoded so they can never drift from
	// the upstream x10 Rust SDK if the type schema changes.
	orderSelector = curve.StarknetKeccak([]byte(
		`"Order"("position_id":"felt","base_asset_id":"AssetId","base_amount":"i64",` +
			`"quote_asset_id":"AssetId","quote_amount":"i64","fee_asset_id":"AssetId",` +
			`"fee_amount":"u64","expiration":"Timestamp","salt":"felt")` +
			`"PositionId"("value":"u32")"AssetId"("value":"felt")"Timestamp"("seconds":"u64")`,
	))
	domainSelector = curve.StarknetKeccak([]byte(
		`"StarknetDomain"("name":"shortstring","version":"shortstring",` +
			`"chainId":"shortstring","revision":"shortstring")`,
	))
	starkNetMsgFelt = encodeShortString("StarkNet Message")
	domainHashCached = curve.PoseidonArray(
		domainSelector, domainName, domainVersion, domainChainID, domainRevision,
	)
	trade.Register("extended", New())
}

// encodeShortString encodes a short string (≤31 chars ASCII) as a Stark felt
// by interpreting the byte sequence big-endian.
func encodeShortString(s string) *felt.Felt {
	bi := new(big.Int).SetBytes([]byte(s))
	return new(felt.Felt).SetBigInt(bi)
}

// ── Market metadata cache (asset IDs + precisions per market) ──────────

type marketMeta struct {
	Name string
	// Synthetic = base asset (BTC, ETH, …) — IDs + resolutions come from
	// l2Config (NOT assetPrecision, which is just UI decimals).
	SyntheticAssetID     *felt.Felt
	SyntheticResolution  int64 // e.g. 1000 for SOL → qty × 1000 = StarkEx fixed-point
	CollateralAssetID    *felt.Felt
	CollateralResolution int64 // e.g. 1000000 for USDC → qty*price × 1e6
	// Default taker fee from the market (decimal as string, e.g. "0.00045")
	TakerFee             float64
	// Tick rules from tradingConfig — required to avoid "Invalid price/qty precision"
	MinPriceChange       float64 // e.g. 0.01 → price must be a multiple
	MinOrderSize         float64 // e.g. 0.1  → min base qty
	MinOrderSizeChange   float64 // e.g. 0.01 → qty step
}

type Adapter struct {
	httpClient *http.Client

	mu         sync.RWMutex
	markets    map[string]marketMeta // key = market name like "BTC-USD"
	marketsAt  time.Time
}

const marketTTL = 30 * time.Minute

func New() *Adapter {
	return &Adapter{
		httpClient: &http.Client{Timeout: 10 * time.Second},
		markets:    make(map[string]marketMeta),
	}
}

func (a *Adapter) Name() string { return "extended" }

// ── HTTP helpers ──────────────────────────────────────────────────────

func (a *Adapter) doGET(ctx context.Context, creds trade.Creds, path string, q map[string]string) (json.RawMessage, error) {
	u := baseURL + path
	if len(q) > 0 {
		u += "?" + trade.SortedFormQuery(q)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return nil, err
	}
	if creds.APIKey != "" {
		req.Header.Set("X-Api-Key", creds.APIKey)
	}
	req.Header.Set("User-Agent", "avalant-fetcher/extended")
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, &trade.Error{Kind: trade.KindTransient, Message: err.Error(), Cause: err}
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return nil, parseError(resp.StatusCode, body)
	}
	return body, nil
}

func (a *Adapter) doPOST(ctx context.Context, creds trade.Creds, path string, body any) (json.RawMessage, error) {
	bodyBytes, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("marshal: %w", err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, baseURL+path, strings.NewReader(string(bodyBytes)))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Api-Key", creds.APIKey)
	req.Header.Set("User-Agent", "avalant-fetcher/extended")
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, &trade.Error{Kind: trade.KindTransient, Message: err.Error(), Cause: err}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		te := parseError(resp.StatusCode, raw)
		// Surface what we sent so 5xx without venue context is debuggable.
		// (venue often returns body=`"Internal Server Error"` which gives
		// no signal about which order field mis-signed.)
		if resp.StatusCode >= 500 {
			snippet := string(bodyBytes)
			if len(snippet) > 800 {
				snippet = snippet[:800] + "...(truncated)"
			}
			te.Message = fmt.Sprintf("HTTP %d %s | sent: %s", resp.StatusCode, te.Message, snippet)
		}
		return nil, te
	}
	return raw, nil
}

func (a *Adapter) doDELETE(ctx context.Context, creds trade.Creds, path string) (json.RawMessage, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodDelete, baseURL+path, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("X-Api-Key", creds.APIKey)
	req.Header.Set("User-Agent", "avalant-fetcher/extended")
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, &trade.Error{Kind: trade.KindTransient, Message: err.Error(), Cause: err}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return nil, parseError(resp.StatusCode, raw)
	}
	return raw, nil
}

// ── Market metadata loader ────────────────────────────────────────────

func (a *Adapter) loadMarkets(ctx context.Context) error {
	a.mu.RLock()
	if time.Since(a.marketsAt) < marketTTL && len(a.markets) > 0 {
		a.mu.RUnlock()
		return nil
	}
	a.mu.RUnlock()

	body, err := a.doGET(ctx, trade.Creds{}, "/info/markets", nil)
	if err != nil {
		return err
	}
	var doc struct {
		Data []struct {
			Name              string `json:"name"`
			Active            bool   `json:"active"`
			Status            string `json:"status"`
			L2Config struct {
				CollateralID         string `json:"collateralId"`
				SyntheticID          string `json:"syntheticId"`
				SyntheticResolution  int64  `json:"syntheticResolution"`
				CollateralResolution int64  `json:"collateralResolution"`
			} `json:"l2Config"`
			TradingConfig struct {
				TakerFee           string `json:"takerFee"`
				MinPriceChange     string `json:"minPriceChange"`
				MinOrderSize       string `json:"minOrderSize"`
				MinOrderSizeChange string `json:"minOrderSizeChange"`
			} `json:"tradingConfig"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &doc); err != nil {
		return errInternal("parse markets", err)
	}
	parseFelt := func(hex string) *felt.Felt {
		f, _ := stark_utils.HexToFelt(strings.TrimPrefix(hex, "0x"))
		if f == nil {
			f = new(felt.Felt)
		}
		return f
	}
	out := make(map[string]marketMeta, len(doc.Data))
	for _, m := range doc.Data {
		if !m.Active {
			continue
		}
		fee, _ := strconv.ParseFloat(m.TradingConfig.TakerFee, 64)
		if fee == 0 {
			fee = 0.00045 // sensible default
		}
		minPx, _ := strconv.ParseFloat(m.TradingConfig.MinPriceChange, 64)
		minSz, _ := strconv.ParseFloat(m.TradingConfig.MinOrderSize, 64)
		minSzChg, _ := strconv.ParseFloat(m.TradingConfig.MinOrderSizeChange, 64)
		out[m.Name] = marketMeta{
			Name:                 m.Name,
			SyntheticAssetID:     parseFelt(m.L2Config.SyntheticID),
			SyntheticResolution:  m.L2Config.SyntheticResolution,
			CollateralAssetID:    parseFelt(m.L2Config.CollateralID),
			CollateralResolution: m.L2Config.CollateralResolution,
			TakerFee:             fee,
			MinPriceChange:       minPx,
			MinOrderSize:         minSz,
			MinOrderSizeChange:   minSzChg,
		}
	}
	a.mu.Lock()
	a.markets = out
	a.marketsAt = time.Now()
	a.mu.Unlock()
	return nil
}

func (a *Adapter) market(name string) (marketMeta, bool) {
	a.mu.RLock()
	defer a.mu.RUnlock()
	m, ok := a.markets[name]
	return m, ok
}

// toMarket maps "BTC" → "BTC-USD" for our caller-facing symbols.
func toMarket(sym string) string {
	s := strings.ToUpper(strings.TrimSpace(sym))
	if strings.Contains(s, "-") {
		return s
	}
	return s + "-USD"
}

// ── Stark signing ─────────────────────────────────────────────────────

// signedFelt converts a (possibly negative) big.Int to a felt. Negative
// values are wrapped via Stark field negation (P - |x| mod P), matching
// the way fast_stark_crypto treats signed StarkAmount in its hash input.
func signedFelt(bi *big.Int) *felt.Felt {
	return new(felt.Felt).SetBigInt(bi)
}

func uintFelt(u uint64) *felt.Felt {
	return new(felt.Felt).SetUint64(u)
}

// signOrder computes the Poseidon hash of the 14-field array (see file
// header) and signs it with the Stark L2 private key. Returns r and s as
// decimal strings — Extended's wire format.
func signOrder(
	privKeyHex string,
	positionID uint64,
	syntheticAssetID *felt.Felt, baseAmount *big.Int,
	collateralAssetID *felt.Felt, quoteAmount *big.Int,
	feeAmount uint64, feeAssetID *felt.Felt,
	expirationSec uint64, salt uint64,
	publicKeyHex string,
) (rDec, sDec string, err error) {
	pubFelt, err := stark_utils.HexToFelt(strings.TrimPrefix(publicKeyHex, "0x"))
	if err != nil {
		return "", "", fmt.Errorf("parse public key: %w", err)
	}
	// Layer 1: Order struct hash. Selector first, then fields in struct order
	// (position_id, base_id, base_amt, quote_id, quote_amt, fee_id, fee_amt,
	// expiration, salt) per x10 Rust reference.
	orderHash := curve.PoseidonArray(
		orderSelector,
		uintFelt(positionID),
		syntheticAssetID,
		signedFelt(baseAmount),
		collateralAssetID,
		signedFelt(quoteAmount),
		feeAssetID,
		uintFelt(feeAmount),
		uintFelt(expirationSec),
		uintFelt(salt),
	)
	// Layer 2: outer message hash composes domain + pubkey + order.
	messageHash := curve.PoseidonArray(
		starkNetMsgFelt,
		domainHashCached,
		pubFelt,
		orderHash,
	)

	priv, ok := new(big.Int).SetString(strings.TrimPrefix(privKeyHex, "0x"), 16)
	if !ok {
		return "", "", fmt.Errorf("parse private key")
	}
	privFelt := new(felt.Felt).SetBigInt(priv)
	r, s, err := curve.SignFelts(messageHash, privFelt)
	if err != nil {
		return "", "", fmt.Errorf("stark sign: %w", err)
	}
	// Venue expects hex-encoded signatures ("0x...") per x10's HexValue
	// type. Decimal strings produce HTTP 500 "Internal Server Error" with
	// no body — easy to mistake for a signing failure (it's parsing).
	return "0x" + r.BigInt(new(big.Int)).Text(16),
		"0x" + s.BigInt(new(big.Int)).Text(16), nil
}

// ── Trade interface ──────────────────────────────────────────────────

type orderSettlement struct {
	Signature          struct {
		R string `json:"r"`
		S string `json:"s"`
	} `json:"signature"`
	StarkKey            string `json:"starkKey"`
	CollateralPosition  string `json:"collateralPosition"`
}

type debugAmounts struct {
	CollateralAmount string `json:"collateralAmount"`
	FeeAmount        string `json:"feeAmount"`
	SyntheticAmount  string `json:"syntheticAmount"`
}

type orderBody struct {
	ID                       string          `json:"id"`
	Market                   string          `json:"market"`
	Type                     string          `json:"type"`
	Side                     string          `json:"side"`
	Qty                      string          `json:"qty"`
	Price                    string          `json:"price"`
	ReduceOnly               bool            `json:"reduceOnly"`
	PostOnly                 bool            `json:"postOnly"`
	TimeInForce              string          `json:"timeInForce"`
	ExpiryEpochMillis        int64           `json:"expiryEpochMillis"`
	Fee                      string          `json:"fee"`
	Nonce                    string          `json:"nonce"`
	SelfTradeProtectionLevel string          `json:"selfTradeProtectionLevel"`
	Settlement               orderSettlement `json:"settlement"`
	DebuggingAmounts         debugAmounts    `json:"debuggingAmounts"`
}

func (a *Adapter) PlaceOrder(ctx context.Context, creds trade.Creds, req trade.OpenRequest) (*trade.Result, error) {
	if err := req.Validate(); err != nil {
		return nil, err
	}
	if creds.APISecret == "" || creds.Wallet == "" || creds.Passphrase == "" {
		return nil, errUser("Extended requires api_key + private_key + public_key + vault (passphrase)")
	}
	if err := a.loadMarkets(ctx); err != nil {
		return nil, err
	}
	market := toMarket(req.Symbol)
	mm, ok := a.market(market)
	if !ok {
		return nil, errUser("Extended market %s not listed", market)
	}

	// Order type → wire type + timeInForce. Market orders are MARKET + IOC
	// with a slippage-padded price for signing; the SDK pads ±0.75% from
	// last price, but we don't have last price here — caller passes 0 for
	// market and we'll resolve from a recent quote.
	var orderType, tif string
	var priceStr string
	switch req.OrderType {
	case trade.OrderLimit:
		orderType = "LIMIT"
		tif = "GTT"
		priceStr = strconv.FormatFloat(req.LimitPrice, 'f', -1, 64)
	case trade.OrderStopMarket, trade.OrderTakeProfitMkt:
		return nil, errUser("stop/TP orders on Extended require TPSL action — not yet implemented; use market or limit")
	default:
		orderType = "MARKET"
		tif = "IOC"
		ref, err := a.lastPrice(ctx, market)
		if err != nil || ref <= 0 {
			return nil, errInternal("get market price for slippage padding", err)
		}
		pad := 1 + marketSlippage
		if req.Side == trade.SideSell {
			pad = 1 - marketSlippage
		}
		// Round to the market's minPriceChange — otherwise Extended
		// rejects with "Invalid price precision". Default 0.01 if unset.
		px := ref * pad
		tick := mm.MinPriceChange
		if tick <= 0 {
			tick = 0.01
		}
		px = math.Round(px/tick) * tick
		// Compute decimal places from the tick (e.g. 0.01 → 2 decimals).
		decs := 0
		t := tick
		for t < 1 && decs < 12 {
			t *= 10
			decs++
		}
		priceStr = strconv.FormatFloat(px, 'f', decs, 64)
	}
	// Round qty DOWN to minOrderSizeChange so the signed quote_amount
	// matches an acceptable order size.
	if mm.MinOrderSizeChange > 0 {
		req.Quantity = math.Floor(req.Quantity/mm.MinOrderSizeChange) * mm.MinOrderSizeChange
	}
	if mm.MinOrderSize > 0 && req.Quantity < mm.MinOrderSize {
		return nil, errUser("Extended: qty %g below min %g for %s",
			req.Quantity, mm.MinOrderSize, market)
	}

	// Compute signed StarkAmounts. Sign convention: base is negative when
	// SELLING synthetic; quote is negative when BUYING synthetic.
	qty := req.Quantity
	price, _ := strconv.ParseFloat(priceStr, 64)
	baseAmountAbs := mulResolution(qty, mm.SyntheticResolution)
	quoteAmountAbs := mulResolution(qty*price, mm.CollateralResolution)
	feeAmount := mulResolution(qty*price*mm.TakerFee, mm.CollateralResolution)
	if feeAmount.Sign() == 0 {
		feeAmount.SetInt64(1) // never sign with zero fee
	}

	baseAmount := new(big.Int).Set(baseAmountAbs)
	quoteAmount := new(big.Int).Set(quoteAmountAbs)
	if req.Side == trade.SideBuy {
		quoteAmount.Neg(quoteAmount)
	} else {
		baseAmount.Neg(baseAmount)
	}

	now := time.Now().UTC()
	expiryMs := now.Add(28 * 24 * time.Hour).UnixMilli() // wire expiry
	expirationSec := uint64(now.Add(28*24*time.Hour).Add(signBufferDays*24*time.Hour).Unix())
	salt := randomNonce()

	positionID, err := strconv.ParseUint(creds.Passphrase, 10, 64)
	if err != nil {
		return nil, errUser("vault must be an integer (Creds.Passphrase): %v", err)
	}

	rDec, sDec, err := signOrder(
		creds.APISecret,
		positionID,
		mm.SyntheticAssetID, baseAmount,
		mm.CollateralAssetID, quoteAmount,
		feeAmount.Uint64(), mm.CollateralAssetID,
		expirationSec, salt,
		creds.Wallet,
	)
	if err != nil {
		return nil, errInternal("sign order", err)
	}

	body := orderBody{
		ID:                       newClientOID(),
		Market:                   market,
		Type:                     orderType,
		Side:                     strings.ToUpper(string(req.Side)),
		Qty:                      strconv.FormatFloat(qty, 'f', -1, 64),
		Price:                    priceStr,
		TimeInForce:              tif,
		ExpiryEpochMillis:        expiryMs,
		Fee:                      strconv.FormatFloat(mm.TakerFee, 'f', -1, 64),
		Nonce:                    strconv.FormatUint(salt, 10),
		SelfTradeProtectionLevel: "ACCOUNT",
	}
	body.Settlement.Signature.R = rDec
	body.Settlement.Signature.S = sDec
	// stark_key is HexValue too — must include the "0x" prefix.
	body.Settlement.StarkKey = "0x" + strings.TrimPrefix(creds.Wallet, "0x")
	body.Settlement.CollateralPosition = creds.Passphrase
	body.DebuggingAmounts.CollateralAmount = quoteAmountAbs.String()
	body.DebuggingAmounts.FeeAmount = feeAmount.String()
	body.DebuggingAmounts.SyntheticAmount = baseAmountAbs.String()

	respBody, err := a.doPOST(ctx, creds, "/user/order", body)
	if err != nil {
		return nil, err
	}
	var resp struct {
		Status string `json:"status"`
		Data   struct {
			ID         string `json:"id"`
			ExternalID string `json:"externalId"`
		} `json:"data"`
	}
	_ = json.Unmarshal(respBody, &resp)
	if resp.Data.ID == "" {
		return nil, errInternal("Extended did not return an order id", fmt.Errorf("body=%s", string(respBody)))
	}
	res := &trade.Result{
		OrderID:       resp.Data.ID,
		ClientOrderID: resp.Data.ExternalID,
		Symbol:        req.Symbol,
		Side:          req.Side,
		Quantity:      qty,
		Status:        "NEW",
		CreatedAt:     time.Now().UTC(),
		Raw:           respBody,
	}
	if req.OrderType.EffectiveMarket() {
		if avg := a.fetchAvgPrice(ctx, creds, resp.Data.ID); avg > 0 {
			res.AvgPrice = avg
		}
	}
	return res, nil
}

func (a *Adapter) fetchAvgPrice(ctx context.Context, creds trade.Creds, orderID string) float64 {
	timer := time.NewTimer(400 * time.Millisecond)
	defer timer.Stop()
	select {
	case <-timer.C:
	case <-ctx.Done():
		return 0
	}
	body, err := a.doGET(ctx, creds, "/user/orders/"+orderID, nil)
	if err != nil {
		return 0
	}
	var doc struct {
		Data struct {
			AveragePrice string `json:"average_price"`
		} `json:"data"`
	}
	_ = json.Unmarshal(body, &doc)
	v, _ := strconv.ParseFloat(doc.Data.AveragePrice, 64)
	return v
}

// lastPrice fetches a recent reference price from the public market info.
func (a *Adapter) lastPrice(ctx context.Context, market string) (float64, error) {
	body, err := a.doGET(ctx, trade.Creds{}, "/info/markets", nil)
	if err != nil {
		return 0, err
	}
	var doc struct {
		Data []struct {
			Name        string `json:"name"`
			MarketStats struct {
				LastPrice string `json:"lastPrice"`
				MarkPrice string `json:"markPrice"`
			} `json:"marketStats"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &doc); err != nil {
		return 0, err
	}
	for _, m := range doc.Data {
		if m.Name != market {
			continue
		}
		px, _ := strconv.ParseFloat(m.MarketStats.MarkPrice, 64)
		if px > 0 {
			return px, nil
		}
		last, _ := strconv.ParseFloat(m.MarketStats.LastPrice, 64)
		return last, nil
	}
	return 0, fmt.Errorf("market %s not in response", market)
}

func (a *Adapter) ClosePosition(ctx context.Context, creds trade.Creds, req trade.CloseRequest) (*trade.Result, error) {
	if req.Symbol == "" {
		return nil, errUser("symbol required")
	}
	positions, err := a.ListPositions(ctx, creds, req.Symbol)
	if err != nil {
		return nil, err
	}
	if len(positions) == 0 {
		return &trade.Result{Symbol: req.Symbol, Status: "FLAT"}, nil
	}
	p := positions[0]
	closeSide := trade.SideSell
	if p.Side == trade.SideSell {
		closeSide = trade.SideBuy
	}
	return a.PlaceOrder(ctx, creds, trade.OpenRequest{
		Symbol:   req.Symbol,
		Side:     closeSide,
		Quantity: p.Quantity,
		// Extended supports reduceOnly via flag; we pad via market price as usual.
	})
}

func (a *Adapter) ListPositions(ctx context.Context, creds trade.Creds, symbol string) ([]trade.Position, error) {
	q := map[string]string{}
	if symbol != "" {
		q["market"] = toMarket(symbol)
	}
	body, err := a.doGET(ctx, creds, "/user/positions", q)
	if err != nil {
		return nil, err
	}
	var doc struct {
		Data []struct {
			Market        string `json:"market"`
			Side          string `json:"side"`
			Size          string `json:"size"`
			AvgEntryPrice string `json:"avgEntryPrice"`
			MarkPrice     string `json:"markPrice"`
			UnrealizedPnl string `json:"unrealizedPnl"`
			Leverage      string `json:"leverage"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &doc); err != nil {
		return nil, errInternal("parse positions", err)
	}
	out := make([]trade.Position, 0, len(doc.Data))
	for _, p := range doc.Data {
		qty, _ := strconv.ParseFloat(p.Size, 64)
		if qty == 0 {
			continue
		}
		sym := strings.TrimSuffix(p.Market, "-USD")
		side := trade.SideBuy
		if strings.ToUpper(p.Side) == "SHORT" || strings.ToUpper(p.Side) == "SELL" {
			side = trade.SideSell
		}
		entry, _ := strconv.ParseFloat(p.AvgEntryPrice, 64)
		mark, _ := strconv.ParseFloat(p.MarkPrice, 64)
		upnl, _ := strconv.ParseFloat(p.UnrealizedPnl, 64)
		lev, _ := strconv.ParseFloat(p.Leverage, 64)
		out = append(out, trade.Position{
			Symbol:        sym,
			Side:          side,
			Quantity:      abs(qty),
			EntryPrice:    entry,
			MarkPrice:     mark,
			UnrealizedPnL: upnl,
			Leverage:      int(lev),
		})
	}
	return out, nil
}

func (a *Adapter) FetchBalance(ctx context.Context, creds trade.Creds) (*trade.Balance, error) {
	body, err := a.doGET(ctx, creds, "/user/balance", nil)
	if err != nil {
		return nil, err
	}
	var doc struct {
		Data struct {
			Balance            string `json:"balance"`
			Equity             string `json:"equity"`
			AvailableForTrade  string `json:"availableForTrade"`
			InitialMargin      string `json:"initialMargin"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &doc); err != nil {
		return nil, errInternal("parse balance", err)
	}
	avail, _ := strconv.ParseFloat(doc.Data.AvailableForTrade, 64)
	total, _ := strconv.ParseFloat(doc.Data.Equity, 64)
	margin, _ := strconv.ParseFloat(doc.Data.InitialMargin, 64)
	return &trade.Balance{
		AvailableUSD: avail,
		TotalUSD:     total,
		MarginUSD:    margin,
	}, nil
}

// GetBalance is the trade.Adapter interface name; we keep FetchBalance as
// an alias for symmetry with our other Go adapters that historically used
// that name. Callers should use either; both return the same Balance.
func (a *Adapter) GetBalance(ctx context.Context, creds trade.Creds) (*trade.Balance, error) {
	return a.FetchBalance(ctx, creds)
}

// SetLeverage is a no-op on Extended — leverage is computed from collateral
// and position size; there's no dedicated knob.
func (a *Adapter) SetLeverage(ctx context.Context, creds trade.Creds, req trade.LeverageRequest) error {
	return nil
}

// ── Helpers ───────────────────────────────────────────────────────────

// mulResolution scales a float quantity by the StarkEx fixed-point
// resolution from /info/markets l2Config (e.g. SOL syntheticResolution=1000,
// USDC collateralResolution=1000000). Output is the big.Int the venue
// recomputes and verifies the signature against.
func mulResolution(v float64, resolution int64) *big.Int {
	if resolution <= 0 {
		return new(big.Int)
	}
	r := new(big.Float).SetFloat64(v)
	r.Mul(r, new(big.Float).SetInt64(resolution))
	out, _ := r.Int(nil)
	if out == nil {
		out = new(big.Int)
	}
	return out
}

func abs(v float64) float64 {
	if v < 0 {
		return -v
	}
	return v
}

func randomNonce() uint64 {
	var buf [8]byte
	_, _ = rand.Read(buf[:])
	return new(big.Int).SetBytes(buf[:]).Uint64()
}

// newClientOID returns a UUID-shaped string suitable for use as the
// `id` (externalId) field. Avoids pulling in the google/uuid module —
// random sha256 prefix formatted as 8-4-4-4-12 is enough for uniqueness.
func newClientOID() string {
	var buf [16]byte
	_, _ = rand.Read(buf[:])
	// Mash with a timestamp so collisions across processes are unlikely.
	now := uint64(time.Now().UnixNano())
	for i := 0; i < 8; i++ {
		buf[i] ^= byte(now >> (i * 8))
	}
	h := sha256.Sum256(buf[:])
	s := hex.EncodeToString(h[:16])
	return s[:8] + "-" + s[8:12] + "-" + s[12:16] + "-" + s[16:20] + "-" + s[20:32]
}

func errUser(format string, args ...any) *trade.Error {
	return &trade.Error{Kind: trade.KindUser, Message: fmt.Sprintf(format, args...)}
}

func errInternal(msg string, cause error) *trade.Error {
	return &trade.Error{Kind: trade.KindInternal, Message: msg, Cause: cause}
}

func parseError(status int, body []byte) *trade.Error {
	var env struct {
		Status string `json:"status"`
		Error  struct {
			Code    int    `json:"code"`
			Message string `json:"message"`
		} `json:"error"`
	}
	_ = json.Unmarshal(body, &env)
	msg := env.Error.Message
	if msg == "" {
		msg = string(body)
		if msg == "" {
			msg = fmt.Sprintf("HTTP %d", status)
		}
	}
	kind := trade.KindInternal
	switch {
	case status == 429:
		kind = trade.KindRateLimit
	case status == 401 || status == 403:
		kind = trade.KindUser
	case status >= 400 && status < 500:
		kind = trade.KindUser
	case status >= 500:
		kind = trade.KindTransient
	}
	return &trade.Error{Kind: kind, Message: msg}
}
