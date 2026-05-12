// trades.go — BingX swap public trade stream (gzipped, custom ping).
//
// dataType: <SYM>-USDT@trade — per-trade events.
// Subscribe: {"id":"X","reqType":"sub","dataType":"BTC-USDT@trade"}
// PingPong: server sends gzipped "Ping" every ~5s, expects "Pong".
//
// Event wire (after gunzip):
//
//	{"code":0,"dataType":"BTC-USDT@trade",
//	 "data":[{"T":1718...,"s":"BTC-USDT","p":"63125.5","q":"0.001","m":true}]}
//
// m=true → buyer was maker → taker SOLD → Sell tick.
package bingx

import (
	"bytes"
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

const tradesWS = "wss://open-api-swap.bingx.com/swap-market"

type Trades struct{}

func NewTrades(onTick ticks.UpdateFunc) *ticks.Runner {
	return ticks.NewRunner(&Trades{}, onTick)
}

func (a *Trades) Name() string                          { return "bingx" }
func (a *Trades) URL(_ context.Context) (string, error) { return tradesWS, nil }

func (a *Trades) BuildSubscribe(symbols []string) [][]byte {
	frames := make([][]byte, 0, len(symbols))
	for i, s := range symbols {
		f := map[string]any{
			"id":       strconv.Itoa(i + 1),
			"reqType":  "sub",
			"dataType": strings.ToUpper(s) + "-USDT@trade",
		}
		b, _ := ticks.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

func (a *Trades) Parse(frame []byte) ([]ticks.Tick, error) {
	var msg struct {
		DataType string `json:"dataType"`
		Data     []struct {
			TsMS int64  `json:"T"`
			S    string `json:"s"`
			P    string `json:"p"`
			Q    string `json:"q"`
			M    bool   `json:"m"`
		} `json:"data"`
	}
	if err := ticks.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if !strings.HasSuffix(msg.DataType, "@trade") || len(msg.Data) == 0 {
		return nil, nil
	}
	pair := strings.SplitN(msg.DataType, "@", 2)[0]
	if !strings.HasSuffix(pair, "-USDT") {
		return nil, nil
	}
	token := strings.TrimSuffix(pair, "-USDT")
	out := make([]ticks.Tick, 0, len(msg.Data))
	for _, d := range msg.Data {
		price, _ := strconv.ParseFloat(d.P, 64)
		size, _ := strconv.ParseFloat(d.Q, 64)
		if price <= 0 || size <= 0 {
			continue
		}
		side := ticks.Buy
		if d.M {
			side = ticks.Sell
		}
		out = append(out, ticks.Tick{
			Exchange: "bingx",
			Symbol:   token,
			Price:    price,
			Size:     size,
			Side:     side,
			TsMS:     d.TsMS,
		})
	}
	if len(out) == 0 {
		return nil, nil
	}
	return out, nil
}

// Same Ping/Pong text dance as the orderbook adapter — see futures.go.
func (a *Trades) Heartbeat() []byte                { return nil }
func (a *Trades) HeartbeatInterval() time.Duration { return 0 }
func (a *Trades) PongFor(frame []byte) []byte {
	if bytes.Equal(bytes.TrimSpace(frame), []byte("Ping")) {
		return []byte("Pong")
	}
	if bytes.Contains(frame, []byte(`"ping"`)) && !bytes.Contains(frame, []byte("dataType")) {
		return []byte(`{"pong":""}`)
	}
	return nil
}
func (a *Trades) UseLibPings() bool             { return false }
func (a *Trades) SubscribeDelay() time.Duration { return 0 }
func (a *Trades) MaxSymbols() int               { return 100 }
func (a *Trades) DecompressGzip() bool          { return true }

func (a *Trades) OnReconnect() {}
