// trades.go — Gate.io USDT-perp public trade stream.
//
// Channel: futures.trades — every individual fill pushed in real time.
// Subscribe shape (per-symbol frame, payload is a list of contracts):
//
//	{"time":...,"channel":"futures.trades","event":"subscribe",
//	 "payload":["BTC_USDT"]}
//
// Event wire:
//
//	{"time":...,"channel":"futures.trades","event":"update",
//	 "result":[{"id":123,"size":100,"price":"63125.5","contract":"BTC_USDT",
//	            "create_time_ms":...,"is_internal":false}]}
//
// size: positive = taker bought, negative = taker sold (Gate convention).
package gate

import (
	"context"
	"encoding/json"
	"math"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

const tradesWS = "wss://fx-ws.gateio.ws/v4/ws/usdt"

type Trades struct{}

func NewTrades(onTick ticks.UpdateFunc) *ticks.Runner {
	return ticks.NewRunner(&Trades{}, onTick)
}

func (a *Trades) Name() string                          { return "gate" }
func (a *Trades) URL(_ context.Context) (string, error) { return tradesWS, nil }

func (a *Trades) BuildSubscribe(symbols []string) [][]byte {
	frames := make([][]byte, 0, len(symbols))
	for _, s := range symbols {
		f := map[string]any{
			"time":    time.Now().Unix(),
			"channel": "futures.trades",
			"event":   "subscribe",
			"payload": []string{strings.ToUpper(s) + "_USDT"},
		}
		b, _ := ticks.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

func (a *Trades) Parse(frame []byte) ([]ticks.Tick, error) {
	// Two-pass parse: Gate's subscribe-ack uses `result: {status: ...}`
	// (object) but data frames use `result: [...]` (array). Sonic
	// strict-typing throws on either if we declare the wrong shape,
	// generating ~200 warn lines per 5min of noise. Stash result as
	// RawMessage, gate on event=="update", then decode the array.
	var env struct {
		Channel string          `json:"channel"`
		Event   string          `json:"event"`
		Result  json.RawMessage `json:"result"`
	}
	if err := ticks.UnmarshalJSON(frame, &env); err != nil {
		return nil, err
	}
	if env.Channel != "futures.trades" || env.Event != "update" || len(env.Result) == 0 {
		return nil, nil
	}
	var rows []struct {
		ID       int64   `json:"id"`
		Size     float64 `json:"size"` // Gate signed: + = buy, - = sell
		Price    string  `json:"price"`
		Contract string  `json:"contract"`
		TsMs     int64   `json:"create_time_ms"`
	}
	if err := ticks.UnmarshalJSON(env.Result, &rows); err != nil {
		return nil, err
	}
	if len(rows) == 0 {
		return nil, nil
	}
	out := make([]ticks.Tick, 0, len(rows))
	for _, d := range rows {
		if !strings.HasSuffix(d.Contract, "_USDT") {
			continue
		}
		price, _ := strconv.ParseFloat(d.Price, 64)
		if price <= 0 || d.Size == 0 {
			continue
		}
		side := ticks.Buy
		if d.Size < 0 {
			side = ticks.Sell
		}
		out = append(out, ticks.Tick{
			Exchange: "gate",
			Symbol:   strings.TrimSuffix(d.Contract, "_USDT"),
			Price:    price,
			Size:     math.Abs(d.Size),
			Side:     side,
			TsMS:     d.TsMs,
			ID:       strconv.FormatInt(d.ID, 10),
		})
	}
	if len(out) == 0 {
		return nil, nil
	}
	return out, nil
}

// Gate uses lib-level WS pings; no app heartbeat.
func (a *Trades) Heartbeat() []byte                { return nil }
func (a *Trades) HeartbeatInterval() time.Duration { return 0 }
func (a *Trades) PongFor(_ []byte) []byte          { return nil }
func (a *Trades) UseLibPings() bool                { return true }
func (a *Trades) SubscribeDelay() time.Duration    { return 0 }
func (a *Trades) MaxSymbols() int                  { return 0 }
func (a *Trades) DecompressGzip() bool             { return false }

func (a *Trades) OnReconnect() {}
