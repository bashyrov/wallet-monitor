// Package bitget — funding adapter for Bitget V2 USDT-FUTURES.
//
// WS:   wss://ws.bitget.com/v2/ws/public
//       channel "ticker", instType "USDT-FUTURES" — push includes
//       fundingRate, lastPr, baseVolume, quoteVolume, nextFundingTime.
// REST: https://api.bitget.com/api/v2/mix/market/tickers?productType=USDT-FUTURES
//
// Same lib-ping + heartbeat fixes as the orderbook adapter (bug #4 + #6
// from PLAN — the Bitget V2 server CLOSES the connection if we don't
// send a literal text "ping" every <30s, AND ignores lib-level WS
// pings). Re-deriving here keeps the funding adapter self-contained.
package bitget

import (
	"context"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const (
	wsURL           = "wss://ws.bitget.com/v2/ws/public"
	restURL         = "https://api.bitget.com/api/v2/mix/market/tickers?productType=USDT-FUTURES"
	restFundRateURL = "https://api.bitget.com/api/v2/mix/market/current-fund-rate?productType=USDT-FUTURES&symbol="
	// Bulk endpoint for per-symbol funding interval (hours as string,
	// e.g. LAB="1", most = "8"). Returns all 675 pairs one call —
	// avoids the 675 × sem=8 × ~150ms per-symbol sweep on
	// current-fund-rate that was timing out under the 10s runner
	// budget and leaving most rows on 8h fallback.
	restContractsURL = "https://api.bitget.com/api/v2/mix/market/contracts?productType=USDT-FUTURES"
)

type Adapter struct {
	fundMu       sync.RWMutex
	fundInterval map[string]int // "LABUSDT" -> hours
}

func New() *Adapter { return &Adapter{} }

// fetchIntervalCache loads /contracts once per BackstopFetch cycle
// (idempotent — no-ops if the cache is already non-nil).
func (a *Adapter) fetchIntervalCache(ctx context.Context) {
	a.fundMu.RLock()
	if a.fundInterval != nil {
		a.fundMu.RUnlock()
		return
	}
	a.fundMu.RUnlock()

	var doc struct {
		Data []struct {
			Symbol       string `json:"symbol"`
			FundInterval string `json:"fundInterval"` // hours as string
		} `json:"data"`
	}
	if err := funding.HTTPGet(ctx, restContractsURL, &doc); err != nil {
		return
	}
	a.fundMu.Lock()
	defer a.fundMu.Unlock()
	a.fundInterval = make(map[string]int, len(doc.Data))
	for _, r := range doc.Data {
		iv, _ := strconv.Atoi(r.FundInterval)
		if iv < 1 {
			iv = 8
		}
		a.fundInterval[r.Symbol] = iv
	}
}

func (a *Adapter) lookupInterval(symbol string) int {
	a.fundMu.RLock()
	defer a.fundMu.RUnlock()
	if iv, ok := a.fundInterval[symbol]; ok {
		return iv
	}
	return 8
}

func (a *Adapter) Name() string                          { return "bitget" }
func (a *Adapter) URL(_ context.Context) (string, error) { return wsURL, nil }

func (a *Adapter) BuildSubscribe(symbols []string) [][]byte {
	args := make([]map[string]string, len(symbols))
	for i, s := range symbols {
		args[i] = map[string]string{
			"instType": "USDT-FUTURES",
			"channel":  "ticker",
			"instId":   strings.ToUpper(s) + "USDT",
		}
	}
	frame := map[string]any{"op": "subscribe", "args": args}
	b, _ := ws.MarshalJSON(frame)
	return [][]byte{b}
}

func (a *Adapter) ParseWS(frame []byte) ([]funding.Tick, error) {
	var msg struct {
		Event string `json:"event"`
		Arg   struct {
			InstType string `json:"instType"`
			Channel  string `json:"channel"`
			InstID   string `json:"instId"`
		} `json:"arg"`
		Data []struct {
			InstID          string `json:"instId"`
			LastPr          string `json:"lastPr"`
			IndexPrice      string `json:"indexPrice"`
			MarkPrice       string `json:"markPrice"`
			FundingRate     string `json:"fundingRate"`
			NextFundingTime string `json:"nextFundingTime"` // string ms; absent from update frames
			QuoteVolume     string `json:"quoteVolume"`
			BaseVolume      string `json:"baseVolume"`
		} `json:"data"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Event != "" || msg.Arg.Channel != "ticker" {
		return nil, nil
	}
	out := make([]funding.Tick, 0, len(msg.Data))
	for _, d := range msg.Data {
		if !strings.HasSuffix(d.InstID, "USDT") {
			continue
		}
		token := strings.TrimSuffix(d.InstID, "USDT")
		rate, _ := strconv.ParseFloat(d.FundingRate, 64)
		mark, _ := strconv.ParseFloat(d.MarkPrice, 64)
		if mark == 0 {
			mark, _ = strconv.ParseFloat(d.LastPr, 64)
		}
		idx, _ := strconv.ParseFloat(d.IndexPrice, 64)
		vol, _ := strconv.ParseFloat(d.QuoteVolume, 64)
		nextMs, _ := strconv.ParseInt(d.NextFundingTime, 10, 64)
		t := funding.Tick{
			Symbol:     token,
			Rate:       rate,
			MarkPrice:  mark,
			IndexPrice: idx,
			Volume24h:  vol,
			// IntervalH NOT set — Bitget's WS payload doesn't carry the
			// per-pair interval; forcing 8 wipes the real value (some
			// pairs are 4h). The store preserves the last non-zero
			// value, so once the REST backstop sets it the WS stops
			// stomping it back to default.
		}
		if nextMs > 0 {
			t.NextFunding = time.UnixMilli(nextMs)
		}
		out = append(out, t)
	}
	return out, nil
}

// Bitget V2 quirks (bug #4 + #6) — mirror orderbook adapter.
func (a *Adapter) Heartbeat() []byte                { return []byte("ping") }
func (a *Adapter) HeartbeatInterval() time.Duration { return 25 * time.Second }
func (a *Adapter) PongFor(_ []byte) []byte          { return nil }
func (a *Adapter) UseLibPings() bool                { return false }
func (a *Adapter) DecompressGzip() bool             { return false }

func (a *Adapter) BackstopFetch(ctx context.Context, symbols []string) ([]funding.Tick, error) {
	// Populate the per-symbol interval cache once (bulk /contracts).
	a.fetchIntervalCache(ctx)

	// Bulk tickers — rate, mark, vol. nextFundingTime is NOT in this response.
	var doc struct {
		Data []struct {
			Symbol      string `json:"symbol"`
			LastPr      string `json:"lastPr"`
			IndexPrice  string `json:"indexPrice"`
			MarkPrice   string `json:"markPrice"`
			FundingRate string `json:"fundingRate"`
			QuoteVolume string `json:"quoteVolume"`
		} `json:"data"`
	}
	if err := funding.HTTPGet(ctx, restURL, &doc); err != nil {
		return nil, err
	}
	byToken := make(map[string]*funding.Tick, len(doc.Data))
	for _, r := range doc.Data {
		if !strings.HasSuffix(r.Symbol, "USDT") {
			continue
		}
		token := strings.TrimSuffix(r.Symbol, "USDT")
		rate, _ := strconv.ParseFloat(r.FundingRate, 64)
		mark, _ := strconv.ParseFloat(r.MarkPrice, 64)
		if mark == 0 {
			mark, _ = strconv.ParseFloat(r.LastPr, 64)
		}
		idx, _ := strconv.ParseFloat(r.IndexPrice, 64)
		vol, _ := strconv.ParseFloat(r.QuoteVolume, 64)
		ivl := a.lookupInterval(r.Symbol)
		byToken[token] = &funding.Tick{
			Symbol:     token,
			Rate:       rate,
			MarkPrice:  mark,
			IndexPrice: idx,
			Volume24h:  vol,
			IntervalH:  float64(ivl),
		}
	}

	// Per-symbol current-fund-rate — nextFunding + real intervalH.
	// Bulk tickers omit nextFundingTime; current-fund-rate provides "nextUpdate".
	// Sweep ALL byToken keys (not just subscribed symbols) so the full
	// ~488 instruments get correct interval + nextFunding, not just ~40.
	allSyms := make([]string, 0, len(byToken))
	for tok := range byToken {
		allSyms = append(allSyms, tok)
	}
	// Merge subscribed symbols in case any aren't in tickers feed.
	seen := make(map[string]struct{}, len(allSyms))
	for _, s := range allSyms {
		seen[strings.ToUpper(s)] = struct{}{}
	}
	for _, s := range symbols {
		u := strings.ToUpper(s)
		if _, ok := seen[u]; !ok {
			allSyms = append(allSyms, u)
			seen[u] = struct{}{}
		}
	}
	// Synthetic nextFunding — Bitget settles at UTC boundaries of the
	// pair's interval (00/08/16 UTC for 8h, 00/04/08/12/16/20 for 4h,
	// every hour for 1h). Interval already came from bulk /contracts,
	// so we can compute next locally without per-symbol REST calls
	// (previous version fired 675 × sem=8 GETs on current-fund-rate
	// which timed out under the 10s runner budget and left the long
	// tail on 8h fallback).
	for _, t := range byToken {
		iv := int(t.IntervalH)
		if iv < 1 {
			iv = 8
		}
		cycle := time.Duration(iv) * time.Hour
		t.NextFunding = time.Now().UTC().Truncate(cycle).Add(cycle)
	}
	_ = symbols // no per-symbol call needed anymore

	out := make([]funding.Tick, 0, len(byToken))
	for _, t := range byToken {
		out = append(out, *t)
	}
	return out, nil
}

func (a *Adapter) BackstopInterval() time.Duration { return 2 * time.Second }
