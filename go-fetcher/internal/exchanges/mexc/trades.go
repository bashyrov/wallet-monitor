// trades.go — MEXC contract sub.deal trade stream.
//
// Channel: sub.deal — every individual trade pushed in real time on
// channel `push.deal`. No throttling at the venue level (live test on
// active perps shows 20-80 events/sec depending on activity).
//
// Subscribe shape (one symbol per frame):
//
//	{"method":"sub.deal","param":{"symbol":"BTC_USDT"}}
//
// Event wire:
//
//	{"channel":"push.deal","symbol":"BTC_USDT","ts":1716...,
//	 "data":{"p":"63125.5","v":100,"T":1,"O":1,"M":1,"t":1716...}}
//
// p = price (string in V1 API); v = size (number of contracts);
// T = taker direction (1=BUY, 2=SELL); t = trade timestamp.
package mexc

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

const tradesWS = "wss://contract.mexc.com/edge"

// Trades is the ticks.Adapter for MEXC contract sub.deal.
type Trades struct{}

// NewTrades returns a ticks.Runner ready to call .Run(ctx) on.
func NewTrades(onTick ticks.UpdateFunc) *ticks.Runner {
	return ticks.NewRunner(&Trades{}, onTick)
}

func (a *Trades) Name() string                          { return "mexc" }
func (a *Trades) URL(_ context.Context) (string, error) { return tradesWS, nil }

func (a *Trades) BuildSubscribe(symbols []string) [][]byte {
	// MEXC contract is strict one-symbol-per-frame for sub.deal — batching
	// silently subscribes only the first. Mirrors sub.depth behaviour.
	frames := make([][]byte, 0, len(symbols))
	for _, s := range symbols {
		f := map[string]any{
			"method": "sub.deal",
			"param":  map[string]any{"symbol": strings.ToUpper(s) + "_USDT"},
		}
		b, _ := ticks.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

func (a *Trades) Parse(frame []byte) ([]ticks.Tick, error) {
	// Live wire shape (per prod logs 2026-05-12):
	//   {"symbol":"BTC_USDT","data":[{"p":80587.8,"v":87,"T":1,"O":3,"M":2,"t":...}]}
	// `data` is an ARRAY (one element typically, but always wrapped).
	// First struct attempt expected a map and 100% of frames failed.
	var msg struct {
		Channel string `json:"channel"`
		Symbol  string `json:"symbol"`
		Data    []struct {
			P float64 `json:"p"` // price
			V float64 `json:"v"` // volume (contracts)
			T int     `json:"T"` // taker dir: 1=buy, 2=sell
			Tm int64  `json:"t"` // trade time (ms)
		} `json:"data"`
	}
	if err := ticks.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	// MEXC sometimes omits the `channel` field for sub.deal pushes;
	// presence of `symbol` + `data[]` is the discriminator.
	if msg.Channel != "" && msg.Channel != "push.deal" {
		return nil, nil
	}
	if !strings.HasSuffix(msg.Symbol, "_USDT") || len(msg.Data) == 0 {
		return nil, nil
	}
	token := strings.TrimSuffix(msg.Symbol, "_USDT")
	out := make([]ticks.Tick, 0, len(msg.Data))
	for _, d := range msg.Data {
		if d.P <= 0 || d.V <= 0 {
			continue
		}
		side := ticks.Buy
		if d.T == 2 {
			side = ticks.Sell
		}
		out = append(out, ticks.Tick{
			Exchange: "mexc",
			Symbol:   token,
			Price:    d.P,
			Size:     d.V,
			Side:     side,
			TsMS:     d.Tm,
		})
	}
	if len(out) == 0 {
		return nil, nil
	}
	return out, nil
}

// MEXC requires {"method":"ping"} every ~20s — same heartbeat as the
// orderbook adapter on the same endpoint.
func (a *Trades) Heartbeat() []byte                { return []byte(`{"method":"ping"}`) }
func (a *Trades) HeartbeatInterval() time.Duration { return 18 * time.Second }
func (a *Trades) PongFor(_ []byte) []byte          { return nil }
func (a *Trades) UseLibPings() bool                { return false }
func (a *Trades) SubscribeDelay() time.Duration    { return 0 }
func (a *Trades) MaxSymbols() int                  { return 0 }
func (a *Trades) DecompressGzip() bool             { return false }

func (a *Trades) OnReconnect() {}

func asFloat(v any) float64 {
	switch x := v.(type) {
	case float64:
		return x
	case string:
		f, _ := strconv.ParseFloat(x, 64)
		return f
	}
	return 0
}

func asInt(v any) int {
	switch x := v.(type) {
	case float64:
		return int(x)
	case int:
		return x
	case string:
		n, _ := strconv.Atoi(x)
		return n
	}
	return 0
}

func asInt64(v any) int64 {
	switch x := v.(type) {
	case float64:
		return int64(x)
	case int64:
		return x
	case int:
		return int64(x)
	case string:
		n, _ := strconv.ParseInt(x, 10, 64)
		return n
	}
	return 0
}
