// trades.go — Aster Pro USDT-perp trade stream (Binance USD-M fork).
//
// Same /ws + SUBSCRIBE pattern as Binance — see ../binance/trades.go for
// the rationale (combined-stream URL + @trade silently closes; bare /ws
// works). Wire format identical, just different host.
package aster

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

const tradesWS = "wss://fstream.asterdex.com/ws"

type Trades struct{}

func NewTrades(onTick ticks.UpdateFunc) *ticks.Runner {
	return ticks.NewRunner(&Trades{}, onTick)
}

func (a *Trades) Name() string                          { return "aster" }
func (a *Trades) URL(_ context.Context) (string, error) { return tradesWS, nil }

func (a *Trades) BuildSubscribe(symbols []string) [][]byte {
	if len(symbols) == 0 {
		return nil
	}
	const chunkSize = 200
	frames := make([][]byte, 0, (len(symbols)+chunkSize-1)/chunkSize)
	id := time.Now().UnixNano()
	for i := 0; i < len(symbols); i += chunkSize {
		end := i + chunkSize
		if end > len(symbols) {
			end = len(symbols)
		}
		params := make([]string, end-i)
		for j, s := range symbols[i:end] {
			params[j] = strings.ToLower(s) + "usdt@trade"
		}
		frame := map[string]any{
			"method": "SUBSCRIBE",
			"params": params,
			"id":     id + int64(i),
		}
		b, _ := ticks.MarshalJSON(frame)
		frames = append(frames, b)
	}
	return frames
}

func (a *Trades) Parse(frame []byte) ([]ticks.Tick, error) {
	// Same e/E case-insensitive collision as Binance — bind both explicitly.
	var ev struct {
		Result *any   `json:"result"`
		EvType string `json:"e"`
		EvTime int64  `json:"E"`
		TT     int64  `json:"T"`
		S      string `json:"s"`
		P      string `json:"p"`
		Q      string `json:"q"`
		Tid    int64  `json:"t"`
		M      bool   `json:"m"`
	}
	_ = ev.EvTime
	if err := ticks.UnmarshalJSON(frame, &ev); err != nil {
		return nil, err
	}
	if ev.Result != nil || ev.EvType == "" || ev.EvType != "trade" {
		return nil, nil
	}
	if !strings.HasSuffix(ev.S, "USDT") {
		return nil, nil
	}
	price, _ := strconv.ParseFloat(ev.P, 64)
	size, _ := strconv.ParseFloat(ev.Q, 64)
	if price <= 0 || size <= 0 {
		return nil, nil
	}
	side := ticks.Buy
	if ev.M {
		side = ticks.Sell
	}
	return []ticks.Tick{{
		Exchange: "aster",
		Symbol:   strings.TrimSuffix(ev.S, "USDT"),
		Price:    price,
		Size:     size,
		Side:     side,
		TsMS:     ev.TT,
		ID:       strconv.FormatInt(ev.Tid, 10),
	}}, nil
}

func (a *Trades) Heartbeat() []byte                { return nil }
func (a *Trades) HeartbeatInterval() time.Duration { return 0 }
func (a *Trades) PongFor(_ []byte) []byte          { return nil }
func (a *Trades) UseLibPings() bool                { return true }
func (a *Trades) SubscribeDelay() time.Duration    { return 0 }
func (a *Trades) MaxSymbols() int                  { return 200 }
func (a *Trades) DecompressGzip() bool             { return false }

func (a *Trades) OnReconnect() {}
