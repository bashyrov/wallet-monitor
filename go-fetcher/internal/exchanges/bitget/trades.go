// trades.go — Bitget mix-v2 USDT-FUTURES public trade stream.
//
// Channel: "trade" + instType:"USDT-FUTURES". Pushes each individual
// fill. Subscribe: {"op":"subscribe","args":[{"instType":"USDT-FUTURES",
// "channel":"trade","instId":"BTCUSDT"}]}
//
// Event wire (per Bitget v2 spec):
//
//	{"action":"snapshot"|"update",
//	 "arg":{"instType":"USDT-FUTURES","channel":"trade","instId":"BTCUSDT"},
//	 "data":[{"ts":"1718...","price":"63125.5","size":"0.001","side":"buy"|"sell",
//	          "tradeId":"..."}]}
package bitget

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

const tradesWS = "wss://ws.bitget.com/v2/ws/public"

type Trades struct{}

func NewTrades(onTick ticks.UpdateFunc) *ticks.Runner {
	return ticks.NewRunner(&Trades{}, onTick)
}

func (a *Trades) Name() string                          { return "bitget" }
func (a *Trades) URL(_ context.Context) (string, error) { return tradesWS, nil }

func (a *Trades) BuildSubscribe(symbols []string) [][]byte {
	// 50 args per frame, mirroring orderbook adapter (200 trips error 30002).
	const chunkSize = 50
	frames := make([][]byte, 0, (len(symbols)+chunkSize-1)/chunkSize)
	for i := 0; i < len(symbols); i += chunkSize {
		end := i + chunkSize
		if end > len(symbols) {
			end = len(symbols)
		}
		args := make([]map[string]string, end-i)
		for j, s := range symbols[i:end] {
			args[j] = map[string]string{
				"instType": "USDT-FUTURES",
				"channel":  "trade",
				"instId":   strings.ToUpper(s) + "USDT",
			}
		}
		b, _ := ticks.MarshalJSON(map[string]any{"op": "subscribe", "args": args})
		frames = append(frames, b)
	}
	return frames
}

func (a *Trades) Parse(frame []byte) ([]ticks.Tick, error) {
	var msg struct {
		Action string `json:"action"`
		Event  string `json:"event"`
		Arg    struct {
			Channel string `json:"channel"`
			InstID  string `json:"instId"`
		} `json:"arg"`
		Data []struct {
			Ts      string `json:"ts"`
			Price   string `json:"price"`
			Size    string `json:"size"`
			Side    string `json:"side"`
			TradeID string `json:"tradeId"`
		} `json:"data"`
	}
	if err := ticks.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Event != "" || msg.Arg.Channel != "trade" || len(msg.Data) == 0 {
		return nil, nil
	}
	if !strings.HasSuffix(msg.Arg.InstID, "USDT") {
		return nil, nil
	}
	token := strings.TrimSuffix(msg.Arg.InstID, "USDT")
	out := make([]ticks.Tick, 0, len(msg.Data))
	for _, d := range msg.Data {
		price, _ := strconv.ParseFloat(d.Price, 64)
		size, _ := strconv.ParseFloat(d.Size, 64)
		if price <= 0 || size <= 0 {
			continue
		}
		side := ticks.Buy
		if d.Side == "sell" {
			side = ticks.Sell
		}
		ts, _ := strconv.ParseInt(d.Ts, 10, 64)
		out = append(out, ticks.Tick{
			Exchange: "bitget",
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

// Bitget — literal "ping" text every 25s, server kicks lib pings.
func (a *Trades) Heartbeat() []byte                { return []byte("ping") }
func (a *Trades) HeartbeatInterval() time.Duration { return 25 * time.Second }
func (a *Trades) PongFor(_ []byte) []byte          { return nil }
func (a *Trades) UseLibPings() bool                { return false }
func (a *Trades) SubscribeDelay() time.Duration    { return 200 * time.Millisecond }
func (a *Trades) MaxSymbols() int                  { return 0 }
func (a *Trades) DecompressGzip() bool             { return false }

func (a *Trades) OnReconnect() {}
