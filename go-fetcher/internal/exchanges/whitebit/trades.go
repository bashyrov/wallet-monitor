// trades.go — WhiteBIT public trade stream.
//
// Method: deals_subscribe — per-deal events on perp markets.
// Subscribe: {"id":1,"method":"deals_subscribe","params":["BTC_PERP"]}
//
// Event wire:
//
//	{"method":"deals_update",
//	 "params":["BTC_PERP",[{"id":...,"time":...,"price":"63125.5",
//	                       "amount":"0.001","type":"buy"|"sell"}]]}
package whitebit

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

const tradesWS = "wss://api.whitebit.com/ws"

type Trades struct{}

func NewTrades(onTick ticks.UpdateFunc) *ticks.Runner {
	return ticks.NewRunner(&Trades{}, onTick)
}

func (a *Trades) Name() string                          { return "whitebit" }
func (a *Trades) URL(_ context.Context) (string, error) { return tradesWS, nil }

func (a *Trades) BuildSubscribe(symbols []string) [][]byte {
	frames := make([][]byte, 0, len(symbols))
	for i, s := range symbols {
		f := map[string]any{
			"id":     i + 1,
			"method": "deals_subscribe",
			"params": []string{strings.ToUpper(s) + "_PERP"},
		}
		b, _ := ticks.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

func (a *Trades) Parse(frame []byte) ([]ticks.Tick, error) {
	var msg struct {
		Method string `json:"method"`
		Params []any  `json:"params"`
	}
	if err := ticks.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Method != "deals_update" || len(msg.Params) < 2 {
		return nil, nil
	}
	market, _ := msg.Params[0].(string)
	if !strings.HasSuffix(market, "_PERP") {
		return nil, nil
	}
	token := strings.TrimSuffix(market, "_PERP")
	rows, ok := msg.Params[1].([]any)
	if !ok {
		return nil, nil
	}
	out := make([]ticks.Tick, 0, len(rows))
	for _, r := range rows {
		row, ok := r.(map[string]any)
		if !ok {
			continue
		}
		var price, amount float64
		switch p := row["price"].(type) {
		case string:
			price, _ = strconv.ParseFloat(p, 64)
		case float64:
			price = p
		}
		switch a := row["amount"].(type) {
		case string:
			amount, _ = strconv.ParseFloat(a, 64)
		case float64:
			amount = a
		}
		if price <= 0 || amount <= 0 {
			continue
		}
		side := ticks.Buy
		if t, ok := row["type"].(string); ok && t == "sell" {
			side = ticks.Sell
		}
		var tsMs int64
		switch tt := row["time"].(type) {
		case float64:
			// WhiteBIT timestamps are typically Unix seconds float
			tsMs = int64(tt * 1000)
		case int64:
			tsMs = tt
		}
		var tid string
		switch idv := row["id"].(type) {
		case string:
			tid = idv
		case float64:
			tid = strconv.FormatInt(int64(idv), 10)
		}
		out = append(out, ticks.Tick{
			Exchange: "whitebit",
			Symbol:   token,
			Price:    price,
			Size:     amount,
			Side:     side,
			TsMS:     tsMs,
			ID:       tid,
		})
	}
	if len(out) == 0 {
		return nil, nil
	}
	return out, nil
}

// WhiteBIT — server times out after 60s of inactivity; lib pings keep alive.
func (a *Trades) Heartbeat() []byte                { return nil }
func (a *Trades) HeartbeatInterval() time.Duration { return 0 }
func (a *Trades) PongFor(_ []byte) []byte          { return nil }
func (a *Trades) UseLibPings() bool                { return true }
func (a *Trades) SubscribeDelay() time.Duration    { return 0 }
func (a *Trades) MaxSymbols() int                  { return 0 }
func (a *Trades) DecompressGzip() bool             { return false }

func (a *Trades) OnReconnect() {}
