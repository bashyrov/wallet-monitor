// trades.go — Hyperliquid L1 perp public trade stream.
//
// Subscription: {"method":"subscribe","subscription":{"type":"trades","coin":"BTC"}}
//
// Event wire:
//
//	{"channel":"trades",
//	 "data":[{"coin":"BTC","side":"A"|"B","px":"60000","sz":"1.5",
//	          "hash":"0x...","time":1718...,"tid":...}]}
//
// side: "A" = ask (taker sold into bid) → Sell tick.
//       "B" = bid (taker bought from ask) → Buy tick.
package hyperliquid

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

const tradesWS = "wss://api.hyperliquid.xyz/ws"

type Trades struct{}

func NewTrades(onTick ticks.UpdateFunc) *ticks.Runner {
	return ticks.NewRunner(&Trades{}, onTick)
}

func (a *Trades) Name() string                          { return "hyperliquid" }
func (a *Trades) URL(_ context.Context) (string, error) { return tradesWS, nil }

func (a *Trades) BuildSubscribe(symbols []string) [][]byte {
	frames := make([][]byte, 0, len(symbols))
	for _, s := range symbols {
		f := map[string]any{
			"method":       "subscribe",
			"subscription": map[string]any{"type": "trades", "coin": strings.ToUpper(s)},
		}
		b, _ := ticks.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

func (a *Trades) Parse(frame []byte) ([]ticks.Tick, error) {
	var msg struct {
		Channel string `json:"channel"`
		Data    []struct {
			Coin string `json:"coin"`
			Side string `json:"side"` // "A" | "B"
			Px   string `json:"px"`
			Sz   string `json:"sz"`
			Time int64  `json:"time"`
			Tid  int64  `json:"tid"`
		} `json:"data"`
	}
	if err := ticks.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Channel != "trades" || len(msg.Data) == 0 {
		return nil, nil
	}
	out := make([]ticks.Tick, 0, len(msg.Data))
	for _, d := range msg.Data {
		price, _ := strconv.ParseFloat(d.Px, 64)
		size, _ := strconv.ParseFloat(d.Sz, 64)
		if price <= 0 || size <= 0 {
			continue
		}
		side := ticks.Buy
		if d.Side == "A" {
			side = ticks.Sell
		}
		out = append(out, ticks.Tick{
			Exchange: "hyperliquid",
			Symbol:   strings.ToUpper(d.Coin),
			Price:    price,
			Size:     size,
			Side:     side,
			TsMS:     d.Time,
			ID:       strconv.FormatInt(d.Tid, 10),
		})
	}
	if len(out) == 0 {
		return nil, nil
	}
	return out, nil
}

func (a *Trades) Heartbeat() []byte                { return nil }
func (a *Trades) HeartbeatInterval() time.Duration { return 0 }
func (a *Trades) PongFor(_ []byte) []byte          { return nil }
func (a *Trades) UseLibPings() bool                { return true }
func (a *Trades) SubscribeDelay() time.Duration    { return 500 * time.Millisecond }
func (a *Trades) MaxSymbols() int                  { return 0 }
func (a *Trades) DecompressGzip() bool             { return false }

func (a *Trades) OnReconnect() {}
