// Package binance — funding adapter for Binance USDT-perp.
//
// WS:   wss://fstream.binance.com/ws/!markPrice@arr@1s — array of all
//       symbols every 1s with mark/funding/next-time.
// REST: https://fapi.binance.com/fapi/v1/premiumIndex — full sweep with
//       same fields. Used as backstop when WS dropouts.
//
// TODO Phase 4 — add the orderbook package's tradingFilter to drop
// SETTLING/BREAK status (NTRN-class — bug #8). Skipped here to avoid
// import-cycle gymnastics; the merged screener feed already filters
// against orderbook state, so this is a polish issue not correctness.
package binance

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const (
	// Combined-stream with BOTH markPrice and ticker — matches Python's
	// funding_ws adapter exactly. Single-stream with just markPrice
	// silently times out from Singapore IP; the dual-stream form works.
	wsURL = "wss://fstream.binance.com/stream?streams=!markPrice@arr@1s/!ticker@arr"
	// /premiumIndex — funding rate + mark price + nextFundingTime. No volume.
	restURL = "https://fapi.binance.com/fapi/v1/premiumIndex"
	// /ticker/24hr — quote volume for the volume column. Required because
	// the `!ticker@arr` WS stream proved unreliable in prod (all 681
	// symbols stuck at volume_24h=0 in funding.binance.json snapshot)
	// and /premiumIndex doesn't carry volume. Without this the screener
	// volume-floor filter dropped Binance rows or reported "$0" volume.
	restTickerURL = "https://fapi.binance.com/fapi/v1/ticker/24hr"
)

type Adapter struct{}

func New() *Adapter { return &Adapter{} }

func (a *Adapter) Name() string                          { return "binance" }
func (a *Adapter) URL(_ context.Context) (string, error) { return wsURL, nil }

// !markPrice@arr@1s subscribes via URL path — no SUBSCRIBE frame needed.
func (a *Adapter) BuildSubscribe(_ []string) [][]byte { return nil }

func (a *Adapter) ParseWS(frame []byte) ([]funding.Tick, error) {
	// Combined-stream wrapper. NOTE: Binance markPrice@arr stream
	// times out on connect from Singapore IP — needs an outbound
	// proxy for full functionality. WS connection establishes
	// but server delivers zero frames for 30s+. Tracked as a
	// separate infra task; until then Binance funding rows are
	// missing from arbitrage.json (~10% of historical opps).
	var wrap struct {
		Stream string           `json:"stream"`
		Data   []map[string]any `json:"data"`
	}
	if err := ws.UnmarshalJSON(frame, &wrap); err != nil {
		return nil, nil
	}

	// !ticker@arr — supplies 24h quote volume (q field). Critical for
	// the volume filter; markPrice stream alone has no volume.
	if strings.Contains(wrap.Stream, "ticker") {
		type tk struct {
			Symbol string `json:"s"`
			Quote  string `json:"q"`
		}
		body, err := ws.MarshalJSON(wrap.Data)
		if err != nil {
			return nil, nil
		}
		var rows []tk
		if err := ws.UnmarshalJSON(body, &rows); err != nil {
			return nil, nil
		}
		out := make([]funding.Tick, 0, len(rows))
		for _, r := range rows {
			if !strings.HasSuffix(r.Symbol, "USDT") {
				continue
			}
			vol, _ := strconv.ParseFloat(r.Quote, 64)
			if vol > 0 {
				out = append(out, funding.Tick{
					Symbol:    strings.TrimSuffix(r.Symbol, "USDT"),
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
	// Re-decode strictly via the typed shape.
	body, err := ws.MarshalJSON(wrap.Data)
	if err != nil {
		return nil, nil
	}
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
	// Parallel ticker fetch for 24h quote volume. Best-effort: if it
	// fails, vol stays at zero — the store preserves last non-zero per
	// symbol so a single transient ticker failure doesn't wipe volume.
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
	out := make([]funding.Tick, 0, len(rows))
	for _, r := range rows {
		if !strings.HasSuffix(r.Symbol, "USDT") {
			continue
		}
		token := strings.TrimSuffix(r.Symbol, "USDT")
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
