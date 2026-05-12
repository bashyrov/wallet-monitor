// trades.go — Binance USDT-perp trade stream.
//
// Channel: <sym>@trade — every individual trade (each fill) pushed in
// real time. Live-confirmed on prod USDT-M (`@aggTrade` is documented
// but returns no frames on fstream.binance.com — appears retired for
// fapi; spot still has it). Hot pairs BTC/ETH emit 50-200 events/sec.
//
// Endpoint: bare `/ws` + SUBSCRIBE method. NOT combined-stream `/stream?streams=`
// — Binance closes the latter with 1006 EOF for @trade specifically
// (tested empirically; @depth20@100ms works on /stream though). The /ws
// endpoint returns events without the `{stream, data}` wrapper —
// straight `{"e":"trade",...}` payloads.
//
// Event wire (direct, no wrapper):
//
//	{"e":"trade","E":..,"T":..,"s":"BTCUSDT",
//	 "t":12345,"p":"0.001","q":"100","b":..,"a":..,"m":true}
//
// m=true  → buyer was maker → taker SOLD into book → Sell tick
// m=false → buyer was taker → taker BOUGHT from book → Buy tick
package binance

import (
	"context"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

// Bare /ws endpoint — see header. SUBSCRIBE-driven, no per-stream URL.
const tradesWSBase = "wss://fstream.binance.com/ws"

// Trades is the ticks.Adapter for Binance @trade.
type Trades struct {
	filter *tradingFilter // reuse the same delisted-symbol filter as orderbook
	mu     sync.Mutex
	syms   []string
}

// NewTrades returns a ticks.Runner ready to call .Run(ctx) on.
func NewTrades(onTick ticks.UpdateFunc) *ticks.Runner {
	a := &Trades{filter: NewFuturesTradingFilter()}
	return ticks.NewRunner(a, onTick)
}

func (a *Trades) Name() string { return "binance" }

// URL — bare /ws endpoint. SUBSCRIBE-driven; no streams in URL.
func (a *Trades) URL(_ context.Context) (string, error) {
	return tradesWSBase, nil
}

func (a *Trades) BuildSubscribe(symbols []string) [][]byte {
	ctx := context.Background()
	listed := make([]string, 0, len(symbols))
	for _, s := range symbols {
		if a.filter.IsTrading(ctx, strings.ToUpper(s)+"USDT") {
			listed = append(listed, s)
		}
	}
	a.mu.Lock()
	a.syms = append(a.syms[:0], listed...)
	a.mu.Unlock()
	if len(listed) == 0 {
		return nil
	}
	// Combined-stream URL already carries subs; emit explicit SUBSCRIBE
	// frames too for add-on-the-fly behaviour (mirrors futures.go pattern).
	const chunkSize = 200
	frames := make([][]byte, 0, (len(listed)+chunkSize-1)/chunkSize)
	id := time.Now().UnixNano()
	for i := 0; i < len(listed); i += chunkSize {
		end := i + chunkSize
		if end > len(listed) {
			end = len(listed)
		}
		params := make([]string, end-i)
		for j, s := range listed[i:end] {
			params[j] = strings.ToLower(s) + "usdt@trade"
		}
		frame := map[string]any{
			"method": "SUBSCRIBE",
			"params": params,
			"id":     id + int64(i),
		}
		b, _ := ticks.MarshalJSON(frame)
		frames = append(frames, b)
	}
	return frames
}

func (a *Trades) Parse(frame []byte) ([]ticks.Tick, error) {
	// /ws endpoint returns events directly (no `{stream, data}` wrapper).
	// Subscribe-ack: {"result":null,"id":N}. Trade: {"e":"trade","E":..,"T":..,"s":..,"t":..,"p":..,"q":..,"m":..}.
	//
	// Tricky JSON shape: keys "e"/"E" and "t"/"T" both exist. Go's json
	// (and sonic) only fall back to case-insensitive when there's no
	// exact tag match. Bind both casings explicitly so neither side
	// triggers the fallback that would type-mismatch.
	var ev struct {
		Result *any   `json:"result"`
		EvType string `json:"e"`  // "trade"
		EvTime int64  `json:"E"`  // event time (parsed but unused — needed so the wire's "E" doesn't get misrouted into the string "e" field)
		TT     int64  `json:"T"`  // trade time (ms)
		S      string `json:"s"`  // symbol
		P      string `json:"p"`  // price
		Q      string `json:"q"`  // qty
		Tid    int64  `json:"t"`  // trade id (lowercase)
		M      bool   `json:"m"`  // is buyer maker
	}
	_ = ev.EvTime // silence the unused-field hint
	if err := ticks.UnmarshalJSON(frame, &ev); err != nil {
		return nil, err
	}
	if ev.Result != nil || ev.EvType == "" {
		return nil, nil
	}
	if ev.EvType != "trade" {
		return nil, nil
	}
	if !strings.HasSuffix(ev.S, "USDT") {
		return nil, nil
	}
	token := strings.TrimSuffix(ev.S, "USDT")
	price, _ := strconv.ParseFloat(ev.P, 64)
	size, _ := strconv.ParseFloat(ev.Q, 64)
	if price <= 0 || size <= 0 {
		return nil, nil
	}
	side := ticks.Buy
	if ev.M {
		// Buyer was maker → taker was the seller → SELL aggression
		side = ticks.Sell
	}
	return []ticks.Tick{{
		Exchange: "binance",
		Symbol:   token,
		Price:    price,
		Size:     size,
		Side:     side,
		TsMS:     ev.TT,
		ID:       strconv.FormatInt(ev.Tid, 10),
	}}, nil
}

// Lifecycle: identical to orderbook adapter.
func (a *Trades) Heartbeat() []byte                { return nil }
func (a *Trades) HeartbeatInterval() time.Duration { return 0 }
func (a *Trades) PongFor(_ []byte) []byte          { return nil }
func (a *Trades) UseLibPings() bool                { return true }
func (a *Trades) SubscribeDelay() time.Duration    { return 0 }
// 200 matches the orderbook cap on the same /stream endpoint — combined-
// stream URL length blows past Binance's hidden limit beyond that and
// triggers 1008 policy violation on connect.
func (a *Trades) MaxSymbols() int                  { return 200 }
func (a *Trades) DecompressGzip() bool             { return false }

func (a *Trades) OnReconnect() {
	// No state to reset — each frame is independent.
}
