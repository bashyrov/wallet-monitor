// trades.go — Upbit spot trade stream.
//
// Same WS endpoint + array subscribe frame as spot.go, type "trade":
//
//	[{"ticket":"<id>"},{"type":"trade","codes":["USDT-BTC",...]},{"format":"DEFAULT"}]
//
// Push:
//
//	{"type":"trade","code":"USDT-BTC","trade_price":60000.0,
//	 "trade_volume":0.01,"ask_bid":"BID","trade_timestamp":...,
//	 "sequential_id":...}
//
// ask_bid "BID" = taker bought, "ASK" = taker sold. Same replace-
// semantics on subscribe frames as the orderbook adapter — cumulative
// code set, full-union frame every time.
package upbit

import (
	"context"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

type Trades struct {
	mu   sync.Mutex
	subs map[string]struct{}
}

func NewTrades(onTick ticks.UpdateFunc) *ticks.Runner {
	a := &Trades{subs: make(map[string]struct{})}
	return ticks.NewRunner(a, onTick)
}

func (a *Trades) Name() string { return "upbit" }

func (a *Trades) URL(_ context.Context) (string, error) { return spotWSURL, nil }

func (a *Trades) BuildSubscribe(symbols []string) [][]byte {
	a.mu.Lock()
	for _, s := range symbols {
		if s != "" {
			a.subs[strings.ToUpper(s)] = struct{}{}
		}
	}
	codes := make([]string, 0, len(a.subs))
	for s := range a.subs {
		codes = append(codes, "USDT-"+s)
	}
	a.mu.Unlock()
	if len(codes) == 0 {
		return nil
	}
	sort.Strings(codes)
	frame := []any{
		map[string]string{"ticket": "avalant-tr-" + strconv.FormatInt(time.Now().UnixNano(), 36)},
		map[string]any{"type": "trade", "codes": codes},
		map[string]string{"format": "DEFAULT"},
	}
	b, _ := ticks.MarshalJSON(frame)
	return [][]byte{b}
}

func (a *Trades) Parse(frame []byte) ([]ticks.Tick, error) {
	var ev struct {
		Type         string  `json:"type"`
		Code         string  `json:"code"`
		TradePrice   float64 `json:"trade_price"`
		TradeVolume  float64 `json:"trade_volume"`
		AskBid       string  `json:"ask_bid"`
		TradeTsMS    int64   `json:"trade_timestamp"`
		SequentialID int64   `json:"sequential_id"`
		Status       string  `json:"status"`
	}
	if err := ticks.UnmarshalJSON(frame, &ev); err != nil {
		return nil, err
	}
	if ev.Type != "trade" {
		return nil, nil // PING reply or non-trade frame
	}
	token, ok := strings.CutPrefix(ev.Code, "USDT-")
	if !ok {
		return nil, nil
	}
	if ev.TradePrice <= 0 || ev.TradeVolume <= 0 {
		return nil, nil
	}
	side := ticks.Buy
	if ev.AskBid == "ASK" {
		side = ticks.Sell
	}
	return []ticks.Tick{{
		Exchange: "upbit",
		Symbol:   token,
		Price:    ev.TradePrice,
		Size:     ev.TradeVolume,
		Side:     side,
		TsMS:     ev.TradeTsMS,
		ID:       strconv.FormatInt(ev.SequentialID, 10),
	}}, nil
}

func (a *Trades) Heartbeat() []byte                { return []byte("PING") }
func (a *Trades) HeartbeatInterval() time.Duration { return 50 * time.Second }
func (a *Trades) PongFor(_ []byte) []byte          { return nil }
func (a *Trades) UseLibPings() bool                { return true }
func (a *Trades) SubscribeDelay() time.Duration    { return 0 }
func (a *Trades) MaxSymbols() int                  { return 200 }
func (a *Trades) DecompressGzip() bool             { return false }

func (a *Trades) OnReconnect() {
	a.mu.Lock()
	a.subs = make(map[string]struct{})
	a.mu.Unlock()
}
