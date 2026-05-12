// trades.go — HTX (Huobi) linear-swap trade detail stream.
//
// Channel: market.<sym>-USDT.trade.detail — every individual fill.
// Subscribe: {"sub":"market.BTC-USDT.trade.detail","id":"X"}
// gzip-compressed inbound; JSON ping/pong on number type (sonic preserves).
//
// Event wire (after gunzip):
//
//	{"ch":"market.BTC-USDT.trade.detail","ts":...,
//	 "tick":{"id":...,"ts":...,
//	          "data":[{"amount":0.001,"ts":...,"id":...,"price":63125.5,
//	                   "direction":"buy"|"sell"}]}}
package htx

import (
	"context"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const tradesWS = "wss://api.hbdm.com/linear-swap-ws"

type Trades struct{}

func NewTrades(onTick ticks.UpdateFunc) *ticks.Runner {
	return ticks.NewRunner(&Trades{}, onTick)
}

func (a *Trades) Name() string                          { return "htx" }
func (a *Trades) URL(_ context.Context) (string, error) { return tradesWS, nil }

func (a *Trades) BuildSubscribe(symbols []string) [][]byte {
	frames := make([][]byte, 0, len(symbols))
	for i, s := range symbols {
		f := map[string]any{
			"sub": "market." + strings.ToUpper(s) + "-USDT.trade.detail",
			"id":  i + 1,
		}
		b, _ := ticks.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

func (a *Trades) Parse(frame []byte) ([]ticks.Tick, error) {
	var msg struct {
		Ch   string `json:"ch"`
		Tick struct {
			Data []struct {
				Price     float64 `json:"price"`
				Amount    float64 `json:"amount"`
				TsMS      int64   `json:"ts"`
				ID        int64   `json:"id"`
				Direction string  `json:"direction"`
			} `json:"data"`
		} `json:"tick"`
	}
	if err := ticks.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if !strings.HasPrefix(msg.Ch, "market.") || !strings.HasSuffix(msg.Ch, ".trade.detail") || len(msg.Tick.Data) == 0 {
		return nil, nil
	}
	pair := strings.TrimSuffix(strings.TrimPrefix(msg.Ch, "market."), ".trade.detail")
	if !strings.HasSuffix(pair, "-USDT") {
		return nil, nil
	}
	token := strings.TrimSuffix(pair, "-USDT")
	out := make([]ticks.Tick, 0, len(msg.Tick.Data))
	for _, d := range msg.Tick.Data {
		if d.Price <= 0 || d.Amount <= 0 {
			continue
		}
		side := ticks.Buy
		if d.Direction == "sell" {
			side = ticks.Sell
		}
		out = append(out, ticks.Tick{
			Exchange: "htx",
			Symbol:   token,
			Price:    d.Price,
			Size:     d.Amount,
			Side:     side,
			TsMS:     d.TsMS,
		})
	}
	if len(out) == 0 {
		return nil, nil
	}
	return out, nil
}

// HTX JSON-number ping (same as orderbook adapter).
func (a *Trades) Heartbeat() []byte                { return nil }
func (a *Trades) HeartbeatInterval() time.Duration { return 0 }
func (a *Trades) PongFor(frame []byte) []byte {
	var msg struct {
		Ping int64 `json:"ping"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil
	}
	if msg.Ping == 0 {
		return nil
	}
	reply, _ := ws.MarshalJSON(map[string]int64{"pong": msg.Ping})
	return reply
}
func (a *Trades) UseLibPings() bool             { return false }
func (a *Trades) SubscribeDelay() time.Duration { return 0 }
func (a *Trades) MaxSymbols() int               { return 0 }
func (a *Trades) DecompressGzip() bool          { return true }

func (a *Trades) OnReconnect() {}
