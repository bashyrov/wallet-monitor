// trades.go — Binance USDT-perp trade stream.
//
// Channel: <sym>@trade — every individual trade (each fill) pushed in
// real time. Live-confirmed on prod USDT-M (`@aggTrade` is documented
// but returns no frames on fstream.binance.com — appears retired for
// fapi; spot still has it). Hot pairs BTC/ETH emit 50-200 events/sec.
//
// Combined-stream URL: same shape as futures.go but with @trade instead
// of @depth20@100ms. Mounted as a separate ws.Runner — one TCP
// connection per stream type per venue keeps the two feeds independent.
//
// Event wire (inside the combined-stream wrapper):
//
//	{"stream":"btcusdt@trade",
//	 "data": {"e":"trade","E":..,"T":..,"s":"BTCUSDT",
//	          "t":12345,"p":"0.001","q":"100","b":..,"a":..,"m":true}}
//
// m=true  → buyer was maker → taker SOLD into book → Sell tick
// m=false → buyer was taker → taker BOUGHT from book → Buy tick
package binance

import (
	"context"
	"encoding/json"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

const tradesCombinedBase = "wss://fstream.binance.com/stream"

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

func (a *Trades) URL(_ context.Context) (string, error) {
	a.mu.Lock()
	syms := a.syms
	a.mu.Unlock()
	if len(syms) == 0 {
		return tradesCombinedBase + "?streams=btcusdt@trade", nil
	}
	parts := make([]string, len(syms))
	for i, s := range syms {
		parts[i] = strings.ToLower(s) + "usdt@trade"
	}
	return tradesCombinedBase + "?streams=" + strings.Join(parts, "/"), nil
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
	var wrap struct {
		Stream string          `json:"stream"`
		Data   json.RawMessage `json:"data"`
		Result *any            `json:"result"`
	}
	if err := ticks.UnmarshalJSON(frame, &wrap); err != nil {
		return nil, err
	}
	if wrap.Result != nil || len(wrap.Data) == 0 {
		return nil, nil
	}

	var ev struct {
		E string `json:"e"` // event type — "trade"
		T int64  `json:"T"` // trade time (ms)
		S string `json:"s"` // symbol
		P string `json:"p"` // price
		Q string `json:"q"` // qty
		Tid int64 `json:"t"` // trade id
		M bool   `json:"m"` // is buyer maker
	}
	if err := ticks.UnmarshalJSON(wrap.Data, &ev); err != nil {
		return nil, err
	}
	if ev.E != "trade" {
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
		TsMS:     ev.T,
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
