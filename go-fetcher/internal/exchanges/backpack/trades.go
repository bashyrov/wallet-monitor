// trades.go — Backpack perp public trade stream.
//
// Stream: trade.<SYMBOL>_USDC_PERP — per-fill events.
// Subscribe: {"method":"SUBSCRIBE","params":["trade.BTC_USDC_PERP",...]}
//
// Event wire:
//
//	{"stream":"trade.BTC_USDC_PERP",
//	 "data":{"e":"trade","E":...,"s":"BTC_USDC_PERP","p":"63125.5",
//	          "q":"0.001","b":"...","a":"...","t":...,"T":...,"m":true}}
//
// m=true → buyer was maker → taker SOLD → Sell tick.
package backpack

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

const tradesWS = "wss://ws.backpack.exchange"

type Trades struct{}

func NewTrades(onTick ticks.UpdateFunc) *ticks.Runner {
	return ticks.NewRunner(&Trades{}, onTick)
}

func (a *Trades) Name() string                          { return "backpack" }
func (a *Trades) URL(_ context.Context) (string, error) { return tradesWS, nil }

func (a *Trades) BuildSubscribe(symbols []string) [][]byte {
	params := make([]string, len(symbols))
	for i, s := range symbols {
		params[i] = "trade." + strings.ToUpper(s) + "_USDC_PERP"
	}
	frame := map[string]any{"method": "SUBSCRIBE", "params": params}
	b, _ := ticks.MarshalJSON(frame)
	return [][]byte{b}
}

func (a *Trades) Parse(frame []byte) ([]ticks.Tick, error) {
	// Same e/E case-insensitive collision handling as Binance — bind both.
	var wrap struct {
		Stream string `json:"stream"`
		Data   struct {
			EvType string `json:"e"`
			EvTime int64  `json:"E"`
			S      string `json:"s"`
			P      string `json:"p"`
			Q      string `json:"q"`
			Tid    int64  `json:"t"`
			TT     int64  `json:"T"`
			M      bool   `json:"m"`
		} `json:"data"`
	}
	_ = wrap.Data.EvTime
	if err := ticks.UnmarshalJSON(frame, &wrap); err != nil {
		return nil, err
	}
	if !strings.HasPrefix(wrap.Stream, "trade.") || wrap.Data.EvType != "trade" {
		return nil, nil
	}
	if !strings.HasSuffix(wrap.Data.S, "_USDC_PERP") {
		return nil, nil
	}
	token := strings.TrimSuffix(wrap.Data.S, "_USDC_PERP")
	price, _ := strconv.ParseFloat(wrap.Data.P, 64)
	size, _ := strconv.ParseFloat(wrap.Data.Q, 64)
	if price <= 0 || size <= 0 {
		return nil, nil
	}
	side := ticks.Buy
	if wrap.Data.M {
		side = ticks.Sell
	}
	return []ticks.Tick{{
		Exchange: "backpack",
		Symbol:   token,
		Price:    price,
		Size:     size,
		Side:     side,
		TsMS:     wrap.Data.TT,
		ID:       strconv.FormatInt(wrap.Data.Tid, 10),
	}}, nil
}

// Backpack — server pings every 60s; lib pings work.
func (a *Trades) Heartbeat() []byte                { return nil }
func (a *Trades) HeartbeatInterval() time.Duration { return 0 }
func (a *Trades) PongFor(_ []byte) []byte          { return nil }
func (a *Trades) UseLibPings() bool                { return true }
func (a *Trades) SubscribeDelay() time.Duration    { return 0 }
func (a *Trades) MaxSymbols() int                  { return 0 }
func (a *Trades) DecompressGzip() bool             { return false }

func (a *Trades) OnReconnect() {}
