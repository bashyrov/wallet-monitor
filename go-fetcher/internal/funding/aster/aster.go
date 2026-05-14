// Package aster — funding adapter for Aster (Binance fork).
//
// Same protocol as Binance but on aster hosts. WS path/REST endpoint
// differ; everything else identical.
//
// /fapi/v1/premiumIndex returns 475+ symbols including tokenized equities
// (SHIELDXXX, BRKB, NFLX, AMD, ORCL, HKD…) that lack /ticker/24hr volume
// — 72 symbols on 2026-05-14. Mirrors the Binance fix: filter via
// exchangeInfo status=TRADING, refreshed every 10 min.
package aster

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
	wsURL   = "wss://fstream.asterdex.com/stream?streams=!markPrice@arr@1s/!ticker@arr"
	restURL = "https://fapi.asterdex.com/fapi/v1/premiumIndex"
	// /ticker/24hr — quote volume. WS ticker stream is unreliable for
	// the long tail of symbols (~30% of Aster rows had volume_usd=0 in
	// prod 2026-05-13 with WS only); REST sweep fills the gap, same as
	// the binance funding adapter.
	restTickerURL    = "https://fapi.asterdex.com/fapi/v1/ticker/24hr"
	restExchangeInfo = "https://fapi.asterdex.com/fapi/v1/exchangeInfo"
)

var (
	_tradingMu     sync.RWMutex
	_tradingSet    map[string]struct{}
	_tradingExpiry time.Time
)

const _tradingTTL = 10 * time.Minute

func _tradingAllowed(token string) bool {
	_tradingMu.RLock()
	defer _tradingMu.RUnlock()
	if _tradingSet == nil {
		return true // cold start — fail open
	}
	_, ok := _tradingSet[token]
	return ok
}

func _refreshTradingSet(ctx context.Context) {
	_tradingMu.RLock()
	fresh := _tradingSet != nil && time.Now().Before(_tradingExpiry)
	_tradingMu.RUnlock()
	if fresh {
		return
	}
	var info struct {
		Symbols []struct {
			Symbol string `json:"symbol"`
			Status string `json:"status"`
		} `json:"symbols"`
	}
	if err := funding.HTTPGet(ctx, restExchangeInfo, &info); err != nil {
		return
	}
	set := make(map[string]struct{}, len(info.Symbols))
	for _, s := range info.Symbols {
		if s.Status == "TRADING" && strings.HasSuffix(s.Symbol, "USDT") {
			set[strings.TrimSuffix(s.Symbol, "USDT")] = struct{}{}
		}
	}
	_tradingMu.Lock()
	_tradingSet = set
	_tradingExpiry = time.Now().Add(_tradingTTL)
	_tradingMu.Unlock()
}

type Adapter struct{}

func New() *Adapter { return &Adapter{} }

func (a *Adapter) Name() string                          { return "aster" }
func (a *Adapter) URL(_ context.Context) (string, error) { return wsURL, nil }
func (a *Adapter) BuildSubscribe(_ []string) [][]byte    { return nil }

func (a *Adapter) ParseWS(frame []byte) ([]funding.Tick, error) {
	// Combined-stream — Aster uses Binance's protocol exactly.
	// markPrice@arr@1s for funding+mark; ticker@arr for volume.
	var wrap struct {
		Stream string           `json:"stream"`
		Data   []map[string]any `json:"data"`
	}
	if err := ws.UnmarshalJSON(frame, &wrap); err != nil {
		return nil, nil
	}

	if strings.Contains(wrap.Stream, "ticker") {
		type tk struct {
			Symbol string `json:"s"`
			Quote  string `json:"q"`
		}
		body, _ := ws.MarshalJSON(wrap.Data)
		var rows []tk
		if err := ws.UnmarshalJSON(body, &rows); err != nil {
			return nil, nil
		}
		out := make([]funding.Tick, 0, len(rows))
		for _, r := range rows {
			if !strings.HasSuffix(r.Symbol, "USDT") {
				continue
			}
			token := strings.TrimSuffix(r.Symbol, "USDT")
			if !_tradingAllowed(token) {
				continue
			}
			vol, _ := strconv.ParseFloat(r.Quote, 64)
			if vol > 0 {
				out = append(out, funding.Tick{
					Symbol:    token,
					Volume24h: vol,
					IntervalH: 8,
				})
			}
		}
		return out, nil
	}

	if !strings.Contains(wrap.Stream, "markPrice") {
		return nil, nil
	}
	type row struct {
		Symbol      string `json:"s"`
		MarkPrice   string `json:"p"`
		IndexPrice  string `json:"i"`
		Rate        string `json:"r"`
		NextFunding int64  `json:"T"`
	}
	body, _ := ws.MarshalJSON(wrap.Data)
	var rows []row
	if err := ws.UnmarshalJSON(body, &rows); err != nil {
		return nil, nil
	}
	out := make([]funding.Tick, 0, len(rows))
	for _, r := range rows {
		if !strings.HasSuffix(r.Symbol, "USDT") {
			continue
		}
		token := strings.TrimSuffix(r.Symbol, "USDT")
		if !_tradingAllowed(token) {
			continue
		}
		mark, _ := strconv.ParseFloat(r.MarkPrice, 64)
		idx, _ := strconv.ParseFloat(r.IndexPrice, 64)
		rate, _ := strconv.ParseFloat(r.Rate, 64)
		out = append(out, funding.Tick{
			Symbol:      token,
			Rate:        rate,
			MarkPrice:   mark,
			IndexPrice:  idx,
			NextFunding: time.UnixMilli(r.NextFunding),
			IntervalH:   8,
		})
	}
	return out, nil
}

func (a *Adapter) Heartbeat() []byte                { return nil }
func (a *Adapter) HeartbeatInterval() time.Duration { return 0 }
func (a *Adapter) PongFor(_ []byte) []byte          { return nil }
func (a *Adapter) UseLibPings() bool                { return true }
func (a *Adapter) DecompressGzip() bool             { return false }

func (a *Adapter) BackstopFetch(ctx context.Context, _ []string) ([]funding.Tick, error) {
	var rows []struct {
		Symbol        string `json:"symbol"`
		MarkPrice     string `json:"markPrice"`
		IndexPrice    string `json:"indexPrice"`
		LastFundRate  string `json:"lastFundingRate"`
		NextFundingTs int64  `json:"nextFundingTime"`
	}
	if err := funding.HTTPGet(ctx, restURL, &rows); err != nil {
		return nil, err
	}
	// Volume fill — best-effort, same shape as binance funding adapter.
	var tickers []struct {
		Symbol      string `json:"symbol"`
		QuoteVolume string `json:"quoteVolume"`
	}
	volBySymbol := make(map[string]float64, len(rows))
	if err := funding.HTTPGet(ctx, restTickerURL, &tickers); err == nil {
		for _, t := range tickers {
			if !strings.HasSuffix(t.Symbol, "USDT") {
				continue
			}
			v, _ := strconv.ParseFloat(t.QuoteVolume, 64)
			if v > 0 {
				volBySymbol[strings.TrimSuffix(t.Symbol, "USDT")] = v
			}
		}
	}
	_refreshTradingSet(ctx)
	out := make([]funding.Tick, 0, len(rows))
	for _, r := range rows {
		if !strings.HasSuffix(r.Symbol, "USDT") {
			continue
		}
		token := strings.TrimSuffix(r.Symbol, "USDT")
		if !_tradingAllowed(token) {
			continue
		}
		mark, _ := strconv.ParseFloat(r.MarkPrice, 64)
		idx, _ := strconv.ParseFloat(r.IndexPrice, 64)
		rate, _ := strconv.ParseFloat(r.LastFundRate, 64)
		out = append(out, funding.Tick{
			Symbol:      token,
			Rate:        rate,
			MarkPrice:   mark,
			IndexPrice:  idx,
			Volume24h:   volBySymbol[token],
			NextFunding: time.UnixMilli(r.NextFundingTs),
			IntervalH:   8,
		})
	}
	return out, nil
}

func (a *Adapter) BackstopInterval() time.Duration { return 2 * time.Second }
