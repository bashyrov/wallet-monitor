// Package bybit — funding adapter for Bybit V5 linear perp.
//
// WS:   wss://stream.bybit.com/v5/public/linear
//       Subscribe: tickers.<SYM>USDT — push includes fundingRate,
//       markPrice, nextFundingTime, volume24h, turnover24h.
// REST: https://api.bybit.com/v5/market/tickers?category=linear — full
//       sweep, used as backstop. Bug #7 covered: WS sometimes pushes
//       partial updates with rate but no volume; REST refills volume.
//
// Funding interval is per-symbol (4h or 8h). Fetched from
// /v5/market/instruments-info?category=linear&type=perpetual and cached.
package bybit

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
	wsURL   = "wss://stream.bybit.com/v5/public/linear"
	restURL = "https://api.bybit.com/v5/market/tickers?category=linear"
	// instruments-info endpoint returns per-symbol metadata including
	// fundingInterval (minutes). Used to build the interval cache.
	instrumentsURL = "https://api.bybit.com/v5/market/instruments-info?category=linear&type=perpetual"
)

type Adapter struct {
	fundMu       sync.RWMutex
	fundInterval map[string]int // "BTCUSDT" -> hours
}

func New() *Adapter { return &Adapter{} }

func (a *Adapter) Name() string                          { return "bybit" }
func (a *Adapter) URL(_ context.Context) (string, error) { return wsURL, nil }

func (a *Adapter) BuildSubscribe(symbols []string) [][]byte {
	args := make([]string, len(symbols))
	for i, s := range symbols {
		args[i] = "tickers." + strings.ToUpper(s) + "USDT"
	}
	frame := map[string]any{"op": "subscribe", "args": args}
	b, _ := ws.MarshalJSON(frame)
	return [][]byte{b}
}

func (a *Adapter) ParseWS(frame []byte) ([]funding.Tick, error) {
	var msg struct {
		Topic string `json:"topic"`
		Type  string `json:"type"`
		Data  struct {
			Symbol           string `json:"symbol"`
			FundingRate      string `json:"fundingRate"`
			MarkPrice        string `json:"markPrice"`
			IndexPrice       string `json:"indexPrice"`
			NextFundingTime  string `json:"nextFundingTime"`
			Volume24h        string `json:"volume24h"`
			Turnover24h      string `json:"turnover24h"`
		} `json:"data"`
		Op string `json:"op"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Op != "" {
		return nil, nil // subscribe ack / pong
	}
	if !strings.HasPrefix(msg.Topic, "tickers.") {
		return nil, nil
	}
	sym := msg.Data.Symbol
	if !strings.HasSuffix(sym, "USDT") {
		return nil, nil
	}
	token := strings.TrimSuffix(sym, "USDT")
	rate, _ := strconv.ParseFloat(msg.Data.FundingRate, 64)
	mark, _ := strconv.ParseFloat(msg.Data.MarkPrice, 64)
	idx, _ := strconv.ParseFloat(msg.Data.IndexPrice, 64)
	turn, _ := strconv.ParseFloat(msg.Data.Turnover24h, 64)
	nextMs, _ := strconv.ParseInt(msg.Data.NextFundingTime, 10, 64)

	ivl := a.lookupInterval(context.Background(), sym)

	tick := funding.Tick{
		Symbol:    token,
		Rate:      rate,
		MarkPrice: mark,
		IndexPrice: idx,
		Volume24h: turn,
		IntervalH: float64(ivl),
	}
	if nextMs > 0 {
		tick.NextFunding = time.UnixMilli(nextMs)
	}
	return []funding.Tick{tick}, nil
}

// Bybit V5 public stream wants app-level {"op":"ping"} every <30s —
// see the orderbook adapter's note for the prod observation behind
// this. Same fix here.
func (a *Adapter) Heartbeat() []byte                { return []byte(`{"op":"ping"}`) }
func (a *Adapter) HeartbeatInterval() time.Duration { return 20 * time.Second }
func (a *Adapter) PongFor(_ []byte) []byte          { return nil }
func (a *Adapter) UseLibPings() bool                { return false }
func (a *Adapter) DecompressGzip() bool             { return false }

func (a *Adapter) BackstopFetch(ctx context.Context, _ []string) ([]funding.Tick, error) {
	// Populate the per-symbol funding-interval cache.
	a.fetchIntervalCache(ctx)

	var doc struct {
		Result struct {
			List []struct {
				Symbol           string `json:"symbol"`
				FundingRate      string `json:"fundingRate"`
				MarkPrice        string `json:"markPrice"`
				IndexPrice       string `json:"indexPrice"`
				NextFundingTime  string `json:"nextFundingTime"`
				Volume24h        string `json:"volume24h"`
				Turnover24h      string `json:"turnover24h"`
			} `json:"list"`
		} `json:"result"`
	}
	if err := funding.HTTPGet(ctx, restURL, &doc); err != nil {
		return nil, err
	}
	out := make([]funding.Tick, 0, len(doc.Result.List))
	for _, r := range doc.Result.List {
		if !strings.HasSuffix(r.Symbol, "USDT") {
			continue
		}
		token := strings.TrimSuffix(r.Symbol, "USDT")
		rate, _ := strconv.ParseFloat(r.FundingRate, 64)
		mark, _ := strconv.ParseFloat(r.MarkPrice, 64)
		idx, _ := strconv.ParseFloat(r.IndexPrice, 64)
		turn, _ := strconv.ParseFloat(r.Turnover24h, 64)
		nextMs, _ := strconv.ParseInt(r.NextFundingTime, 10, 64)
		ivl := a.lookupInterval(ctx, r.Symbol)
		tick := funding.Tick{
			Symbol:    token,
			Rate:      rate,
			MarkPrice: mark,
			IndexPrice: idx,
			Volume24h: turn,
			IntervalH: float64(ivl),
		}
		if nextMs > 0 {
			tick.NextFunding = time.UnixMilli(nextMs)
		}
		out = append(out, tick)
	}
	return out, nil
}

// fetchIntervalCache fetches /instruments-info once and populates
// a.fundInterval. Iterates the paginated response — Bybit caps at
// 500 rows per page, cursor drives the rest. Total ~700 USDT-perp
// as of 2026-07 so 2 pages suffice.
func (a *Adapter) fetchIntervalCache(ctx context.Context) {
	a.fundMu.RLock()
	if a.fundInterval != nil {
		a.fundMu.RUnlock()
		return
	}
	a.fundMu.RUnlock()

	agg := make(map[string]int, 800)
	cursor := ""
	for pages := 0; pages < 10; pages++ {
		u := instrumentsURL + "&limit=1000"
		if cursor != "" {
			u += "&cursor=" + cursor
		}
		var doc struct {
			Result struct {
				List []struct {
					Symbol string `json:"symbol"`
					// Bybit returns fundingInterval as a JSON number
					// (minutes) — earlier version had `string` which
					// unmarshalled to "" and fell through to 8h for
					// every row. That silently downgraded top-30
					// USDT-perp APR by 2x.
					FundingInterval int `json:"fundingInterval"`
				} `json:"list"`
				NextPageCursor string `json:"nextPageCursor"`
			} `json:"result"`
		}
		if err := funding.HTTPGet(ctx, u, &doc); err != nil {
			return
		}
		for _, r := range doc.Result.List {
			hours := r.FundingInterval / 60
			if hours < 1 {
				hours = 8
			}
			agg[r.Symbol] = hours
		}
		if doc.Result.NextPageCursor == "" || len(doc.Result.List) == 0 {
			break
		}
		cursor = doc.Result.NextPageCursor
	}
	a.fundMu.Lock()
	defer a.fundMu.Unlock()
	a.fundInterval = agg
}

// lookupInterval returns the per-symbol funding interval in hours.
// Lazily triggers fetchIntervalCache if the cache hasn't been populated.
func (a *Adapter) lookupInterval(ctx context.Context, symbol string) int {
	a.fundMu.RLock()
	if a.fundInterval != nil {
		defer a.fundMu.RUnlock()
		if ivl, ok := a.fundInterval[symbol]; ok {
			return ivl
		}
		return 8
	}
	a.fundMu.RUnlock()

	// Cold start — fill the cache before falling through.
	a.fetchIntervalCache(ctx)

	a.fundMu.RLock()
	defer a.fundMu.RUnlock()
	if ivl, ok := a.fundInterval[symbol]; ok {
		return ivl
	}
	return 8
}

func (a *Adapter) BackstopInterval() time.Duration { return 2 * time.Second }
