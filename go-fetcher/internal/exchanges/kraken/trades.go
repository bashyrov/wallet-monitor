// trades.go — Kraken Futures trade stream.
//
// Feed: trade — every fill on the perp. Subscribe:
//
//	{"event":"subscribe","feed":"trade","product_ids":["PF_XBTUSD",...]}
//
// Event wire:
//
//	{"feed":"trade","product_id":"PF_XBTUSD","uid":"...","side":"buy"|"sell",
//	 "type":"fill"|"liquidation","seq":...,"time":...,"qty":0.001,"price":63125.5}
package kraken

import (
	"context"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

const tradesWS = "wss://futures.kraken.com/ws/v1"

type Trades struct{}

func NewTrades(onTick ticks.UpdateFunc) *ticks.Runner {
	return ticks.NewRunner(&Trades{}, onTick)
}

func (a *Trades) Name() string                          { return "kraken" }
func (a *Trades) URL(_ context.Context) (string, error) { return tradesWS, nil }

func (a *Trades) BuildSubscribe(symbols []string) [][]byte {
	products := make([]string, len(symbols))
	for i, s := range symbols {
		token := strings.ToUpper(s)
		if token == "BTC" {
			token = "XBT"
		}
		products[i] = "PF_" + token + "USD"
	}
	frame := map[string]any{
		"event":       "subscribe",
		"feed":        "trade",
		"product_ids": products,
	}
	b, _ := ticks.MarshalJSON(frame)
	return [][]byte{b}
}

func (a *Trades) Parse(frame []byte) ([]ticks.Tick, error) {
	var msg struct {
		Feed      string  `json:"feed"`
		Event     string  `json:"event"`
		ProductID string  `json:"product_id"`
		Side      string  `json:"side"`
		Qty       float64 `json:"qty"`
		Price     float64 `json:"price"`
		TimeMs    int64   `json:"time"`
		UID       string  `json:"uid"`
	}
	if err := ticks.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Event != "" || msg.Feed != "trade" {
		return nil, nil
	}
	pid := msg.ProductID
	if !strings.HasPrefix(pid, "PF_") || !strings.HasSuffix(pid, "USD") {
		return nil, nil
	}
	token := strings.TrimSuffix(strings.TrimPrefix(pid, "PF_"), "USD")
	if token == "XBT" {
		token = "BTC"
	}
	if msg.Price <= 0 || msg.Qty <= 0 {
		return nil, nil
	}
	side := ticks.Buy
	if msg.Side == "sell" {
		side = ticks.Sell
	}
	return []ticks.Tick{{
		Exchange: "kraken",
		Symbol:   token,
		Price:    msg.Price,
		Size:     msg.Qty,
		Side:     side,
		TsMS:     msg.TimeMs,
		ID:       msg.UID,
	}}, nil
}

// Kraken Futures — lib WS pings work.
func (a *Trades) Heartbeat() []byte                { return nil }
func (a *Trades) HeartbeatInterval() time.Duration { return 0 }
func (a *Trades) PongFor(_ []byte) []byte          { return nil }
func (a *Trades) UseLibPings() bool                { return true }
func (a *Trades) SubscribeDelay() time.Duration    { return 0 }
func (a *Trades) MaxSymbols() int                  { return 0 }
func (a *Trades) DecompressGzip() bool             { return false }

func (a *Trades) OnReconnect() {}
