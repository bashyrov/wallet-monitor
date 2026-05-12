// Package extended — Extended (x10) Starknet-based perp DEX.
//
// Trade-stream WS only — no orderbook adapter exists in this package
// yet (Extended orderbook lives in internal/trade/extended for the trade
// engine path, not the screener path).
//
// Endpoint: path-based, ONE WS per market (Binance-style).
//
//	wss://api.starknet.extended.exchange/stream.extended.exchange/v1/publicTrades/{market}
//
// Omitting `{market}` is documented to fan out ALL markets on one socket
// — we use that form when we have >1 subscribed symbol to avoid one TCP
// connection per coin. Server pushes:
//
//	{"ts":...,"seq":N,
//	 "data":[{"m":"BTC-USD","S":"BUY"|"SELL","tT":"TRADE"|"LIQUIDATION"|"DELEVERAGE",
//	          "T":..,"p":"63125.5","q":"0.001","i":12345}]}
//
// `tT` discriminates: TRADE = normal fill, others are liquidation events.
// We treat all three as ticks for arb purposes.
package extended

import (
	"context"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

const tradesBase = "wss://api.starknet.extended.exchange/stream.extended.exchange/v1/publicTrades"

type Trades struct {
	mu   sync.Mutex
	syms []string
}

func NewTrades(onTick ticks.UpdateFunc) *ticks.Runner {
	return ticks.NewRunner(&Trades{}, onTick)
}

func (a *Trades) Name() string { return "extended" }

func (a *Trades) URL(_ context.Context) (string, error) {
	a.mu.Lock()
	n := len(a.syms)
	a.mu.Unlock()
	if n == 0 {
		// First dial before SetSymbols: subscribe to all markets (one
		// conn, server fans out). Symbol manager filters per-pair on
		// the Hub side anyway.
		return tradesBase, nil
	}
	// We can't include multiple markets in the path; omit market = all.
	// One conn is plenty for the venue's full trade volume.
	return tradesBase, nil
}

func (a *Trades) BuildSubscribe(symbols []string) [][]byte {
	a.mu.Lock()
	a.syms = append(a.syms[:0], symbols...)
	a.mu.Unlock()
	// Path-based subscription — no SUBSCRIBE frame needed.
	return nil
}

func (a *Trades) Parse(frame []byte) ([]ticks.Tick, error) {
	var msg struct {
		Ts   int64 `json:"ts"`
		Seq  int64 `json:"seq"`
		Data []struct {
			Market string `json:"m"`
			Side   string `json:"S"`
			TT     string `json:"tT"`
			T      int64  `json:"T"`
			P      string `json:"p"`
			Q      string `json:"q"`
			I      int64  `json:"i"`
		} `json:"data"`
	}
	if err := ticks.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if len(msg.Data) == 0 {
		return nil, nil
	}
	out := make([]ticks.Tick, 0, len(msg.Data))
	for _, d := range msg.Data {
		if !strings.HasSuffix(d.Market, "-USD") {
			continue
		}
		token := strings.TrimSuffix(d.Market, "-USD")
		price, _ := strconv.ParseFloat(d.P, 64)
		size, _ := strconv.ParseFloat(d.Q, 64)
		if price <= 0 || size <= 0 {
			continue
		}
		side := ticks.Buy
		if d.Side == "SELL" {
			side = ticks.Sell
		}
		out = append(out, ticks.Tick{
			Exchange: "extended",
			Symbol:   token,
			Price:    price,
			Size:     size,
			Side:     side,
			TsMS:     d.T,
			ID:       strconv.FormatInt(d.I, 10),
		})
	}
	if len(out) == 0 {
		return nil, nil
	}
	return out, nil
}

// Extended server pings every ~15s; lib pings handle it.
func (a *Trades) Heartbeat() []byte                { return nil }
func (a *Trades) HeartbeatInterval() time.Duration { return 0 }
func (a *Trades) PongFor(_ []byte) []byte          { return nil }
func (a *Trades) UseLibPings() bool                { return true }
func (a *Trades) SubscribeDelay() time.Duration    { return 0 }
func (a *Trades) MaxSymbols() int                  { return 0 }
func (a *Trades) DecompressGzip() bool             { return false }

func (a *Trades) OnReconnect() {}
