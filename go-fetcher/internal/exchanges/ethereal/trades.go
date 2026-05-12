// Package ethereal — Ethereal perp DEX, public trade stream only.
//
// Endpoint: wss://ws2.ethereal.trade/v1/stream
//
// Previously we thought Ethereal's public WS rejected all subs ("Invalid
// stream subscription type") — that was the Socket.IO transport. The
// raw WS path at /v1/stream accepts the same SDK type names. The
// ethereal-py SDK 0.1.4 wraps this transport at:
//   venv/lib/python3.13/site-packages/ethereal/ws/transports.py
//
// Subscribe: {"event":"subscribe","data":{"type":"TradeFill","symbol":"BTCUSD"}}
//
// Event wire:
//
//	{"e":"TradeFill","t":<ms>,
//	 "data":{"s":"BTCUSD","t":<ms>,
//	         "d":[{"id":"<uuid>","px":"63125.5","sz":"0.001",
//	               "sd":0|1,"sids":["<uuid>","<uuid>"]}]}}
//
// sd: 0/1 — taker side. Convention uncertain; we treat 0 = Buy (taker
// aggressor) and 1 = Sell. First live frames will reveal if reversed
// and we can flip in a follow-up.
//
// Symbol form on Ethereal is `<BASE>USD` (no separator, USD-margined).
package ethereal

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

const tradesWS = "wss://ws2.ethereal.trade/v1/stream"

type Trades struct{}

func NewTrades(onTick ticks.UpdateFunc) *ticks.Runner {
	return ticks.NewRunner(&Trades{}, onTick)
}

func (a *Trades) Name() string                          { return "ethereal" }
func (a *Trades) URL(_ context.Context) (string, error) { return tradesWS, nil }

func (a *Trades) BuildSubscribe(symbols []string) [][]byte {
	frames := make([][]byte, 0, len(symbols))
	for _, s := range symbols {
		f := map[string]any{
			"event": "subscribe",
			"data": map[string]any{
				"type":   "TradeFill",
				"symbol": strings.ToUpper(s) + "USD",
			},
		}
		b, _ := ticks.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

func (a *Trades) Parse(frame []byte) ([]ticks.Tick, error) {
	var msg struct {
		E    string `json:"e"`
		Data struct {
			S string `json:"s"`
			D []struct {
				ID string `json:"id"`
				Px string `json:"px"`
				Sz string `json:"sz"`
				Sd int    `json:"sd"`
				T  int64  `json:"t"`
			} `json:"d"`
			T int64 `json:"t"`
		} `json:"data"`
	}
	if err := ticks.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.E != "TradeFill" || !strings.HasSuffix(msg.Data.S, "USD") || len(msg.Data.D) == 0 {
		return nil, nil
	}
	token := strings.TrimSuffix(msg.Data.S, "USD")
	out := make([]ticks.Tick, 0, len(msg.Data.D))
	for _, d := range msg.Data.D {
		price, _ := strconv.ParseFloat(d.Px, 64)
		size, _ := strconv.ParseFloat(d.Sz, 64)
		if price <= 0 || size <= 0 {
			continue
		}
		side := ticks.Buy
		if d.Sd == 1 {
			side = ticks.Sell
		}
		ts := d.T
		if ts == 0 {
			ts = msg.Data.T
		}
		out = append(out, ticks.Tick{
			Exchange: "ethereal",
			Symbol:   token,
			Price:    price,
			Size:     size,
			Side:     side,
			TsMS:     ts,
			ID:       d.ID,
		})
	}
	if len(out) == 0 {
		return nil, nil
	}
	return out, nil
}

// Heartbeat: not documented; lib ping should keep alive.
func (a *Trades) Heartbeat() []byte                { return nil }
func (a *Trades) HeartbeatInterval() time.Duration { return 0 }
func (a *Trades) PongFor(_ []byte) []byte          { return nil }
func (a *Trades) UseLibPings() bool                { return true }
func (a *Trades) SubscribeDelay() time.Duration    { return 0 }
func (a *Trades) MaxSymbols() int                  { return 0 }
func (a *Trades) DecompressGzip() bool             { return false }

func (a *Trades) OnReconnect() {}
