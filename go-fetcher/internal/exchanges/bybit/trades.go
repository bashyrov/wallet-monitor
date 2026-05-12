// trades.go — Bybit V5 linear-perp public trade stream.
//
// Channel: publicTrade.<SYM>USDT — every individual fill pushed in real
// time. No throttling; hot pairs BTC/ETH emit 30-100+ events/sec.
//
// Subscribe shape (one topic per frame — same defensive pattern as the
// orderbook adapter, so a bad symbol doesn't reject the whole frame):
//
//	{"op":"subscribe","args":["publicTrade.BTCUSDT"]}
//
// Event wire:
//
//	{"topic":"publicTrade.BTCUSDT","type":"snapshot","ts":...,
//	 "data":[{"T":1718...,"s":"BTCUSDT","S":"Buy"|"Sell",
//	          "v":"0.001","p":"70000.5","L":"PlusTick","i":"...",...}]}
//
// S = taker side: "Buy" = taker bought from book → Buy tick.
package bybit

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

const tradesWS = "wss://stream.bybit.com/v5/public/linear"

type Trades struct{}

func NewTrades(onTick ticks.UpdateFunc) *ticks.Runner {
	return ticks.NewRunner(&Trades{}, onTick)
}

func (a *Trades) Name() string                          { return "bybit" }
func (a *Trades) URL(_ context.Context) (string, error) { return tradesWS, nil }

func (a *Trades) BuildSubscribe(symbols []string) [][]byte {
	// One topic per frame (mirrors orderbook adapter — see comment in
	// futures.go about Bybit's all-or-nothing args[] failure mode).
	frames := make([][]byte, 0, len(symbols))
	for _, s := range symbols {
		frame := map[string]any{
			"op":   "subscribe",
			"args": []string{"publicTrade." + strings.ToUpper(s) + "USDT"},
		}
		b, _ := ticks.MarshalJSON(frame)
		frames = append(frames, b)
	}
	return frames
}

func (a *Trades) Parse(frame []byte) ([]ticks.Tick, error) {
	var msg struct {
		Topic string `json:"topic"`
		Data  []struct {
			TsMS int64  `json:"T"` // trade time
			S    string `json:"s"` // symbol e.g. "BTCUSDT"
			Side string `json:"S"` // "Buy" | "Sell"
			V    string `json:"v"` // qty (string)
			P    string `json:"p"` // price (string)
			Tid  string `json:"i"` // trade id
		} `json:"data"`
	}
	if err := ticks.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if !strings.HasPrefix(msg.Topic, "publicTrade.") || len(msg.Data) == 0 {
		return nil, nil
	}
	out := make([]ticks.Tick, 0, len(msg.Data))
	for _, d := range msg.Data {
		if !strings.HasSuffix(d.S, "USDT") {
			continue
		}
		price, _ := strconv.ParseFloat(d.P, 64)
		size, _ := strconv.ParseFloat(d.V, 64)
		if price <= 0 || size <= 0 {
			continue
		}
		side := ticks.Buy
		if d.Side == "Sell" {
			side = ticks.Sell
		}
		out = append(out, ticks.Tick{
			Exchange: "bybit",
			Symbol:   strings.TrimSuffix(d.S, "USDT"),
			Price:    price,
			Size:     size,
			Side:     side,
			TsMS:     d.TsMS,
			ID:       d.Tid,
		})
	}
	if len(out) == 0 {
		return nil, nil
	}
	return out, nil
}

// Bybit V5 uses {"op":"ping"} every 20s.
func (a *Trades) Heartbeat() []byte                { return []byte(`{"op":"ping"}`) }
func (a *Trades) HeartbeatInterval() time.Duration { return 18 * time.Second }
func (a *Trades) PongFor(_ []byte) []byte          { return nil }
func (a *Trades) UseLibPings() bool                { return false }
func (a *Trades) SubscribeDelay() time.Duration    { return 0 }
func (a *Trades) MaxSymbols() int                  { return 0 }
func (a *Trades) DecompressGzip() bool             { return false }

func (a *Trades) OnReconnect() {}
