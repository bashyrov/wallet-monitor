// trades.go — OKX V5 trade stream.
//
// Channel: trades (public, no auth). instId form `<BASE>-USDT-SWAP`
// for perp, `<BASE>-USDT` for spot. Pushes each individual fill.
//
// Subscribe shape:
//
//	{"op":"subscribe","args":[{"channel":"trades","instId":"BTC-USDT-SWAP"}]}
//
// Event wire:
//
//	{"arg":{"channel":"trades","instId":"BTC-USDT-SWAP"},
//	 "data":[{"instId":"BTC-USDT-SWAP","tradeId":"...","px":"63125.5",
//	          "sz":"0.001","side":"buy"|"sell","ts":"1718..."}]}
//
// Side: "buy" = taker bought from book → Buy tick.
package okx

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

const tradesWS = "wss://ws.okx.com:8443/ws/v5/public"

type Trades struct {
	instSuffix string // "-USDT-SWAP" for futures
	exName     string // "okx"
}

func NewTrades(onTick ticks.UpdateFunc) *ticks.Runner {
	return ticks.NewRunner(&Trades{instSuffix: "-USDT-SWAP", exName: "okx"}, onTick)
}

func (a *Trades) Name() string                          { return a.exName }
func (a *Trades) URL(_ context.Context) (string, error) { return tradesWS, nil }

func (a *Trades) BuildSubscribe(symbols []string) [][]byte {
	const chunkSize = 100
	frames := make([][]byte, 0, (len(symbols)+chunkSize-1)/chunkSize)
	for i := 0; i < len(symbols); i += chunkSize {
		end := i + chunkSize
		if end > len(symbols) {
			end = len(symbols)
		}
		args := make([]map[string]string, end-i)
		for j, s := range symbols[i:end] {
			args[j] = map[string]string{
				"channel": "trades",
				"instId":  strings.ToUpper(s) + a.instSuffix,
			}
		}
		b, _ := ticks.MarshalJSON(map[string]any{"op": "subscribe", "args": args})
		frames = append(frames, b)
	}
	return frames
}

func (a *Trades) Parse(frame []byte) ([]ticks.Tick, error) {
	var msg struct {
		Event string `json:"event"`
		Arg   struct {
			Channel string `json:"channel"`
		} `json:"arg"`
		Data []struct {
			InstID  string `json:"instId"`
			TradeID string `json:"tradeId"`
			Px      string `json:"px"`
			Sz      string `json:"sz"`
			Side    string `json:"side"`
			Ts      string `json:"ts"`
		} `json:"data"`
	}
	if err := ticks.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Event != "" || msg.Arg.Channel != "trades" || len(msg.Data) == 0 {
		return nil, nil
	}
	out := make([]ticks.Tick, 0, len(msg.Data))
	for _, d := range msg.Data {
		if !strings.HasSuffix(d.InstID, a.instSuffix) {
			continue
		}
		token := strings.TrimSuffix(d.InstID, a.instSuffix)
		price, _ := strconv.ParseFloat(d.Px, 64)
		size, _ := strconv.ParseFloat(d.Sz, 64)
		if price <= 0 || size <= 0 {
			continue
		}
		side := ticks.Buy
		if d.Side == "sell" {
			side = ticks.Sell
		}
		ts, _ := strconv.ParseInt(d.Ts, 10, 64)
		out = append(out, ticks.Tick{
			Exchange: a.exName,
			Symbol:   token,
			Price:    price,
			Size:     size,
			Side:     side,
			TsMS:     ts,
			ID:       d.TradeID,
		})
	}
	if len(out) == 0 {
		return nil, nil
	}
	return out, nil
}

// OKX V5 — same `"ping"`/`"pong"` text frame heartbeat as the orderbook
// adapter on the same endpoint.
func (a *Trades) Heartbeat() []byte                { return []byte("ping") }
func (a *Trades) HeartbeatInterval() time.Duration { return 25 * time.Second }
func (a *Trades) PongFor(_ []byte) []byte          { return nil }
func (a *Trades) UseLibPings() bool                { return false }
func (a *Trades) SubscribeDelay() time.Duration    { return 0 }
func (a *Trades) MaxSymbols() int                  { return 0 }
func (a *Trades) DecompressGzip() bool             { return false }

func (a *Trades) OnReconnect() {}
