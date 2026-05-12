// trades.go — Lighter public trade stream.
//
// Channel: trade/<market_id> — every fill on the perp. Same WS host as
// the orderbook adapter; same symbol→market_id resolution path via
// the shared idMap.
//
// Subscribe: {"type":"subscribe","channel":"trade/0"}    (BTC = 0)
//
// Event wire (per Lighter docs):
//
//	{"channel":"trade:0","type":"update/trade","nonce":<int>,
//	 "trades":[{"trade_id":..,"market_id":0,"size":"...","price":"...",
//	            "is_maker_ask":true|false,"timestamp":<ms>,
//	            "type":"trade"|"liquidation"|"deleverage"|"market-settlement",
//	            ...}],
//	 "liquidation_trades":[Trade,...]}
//
// Side semantics (docs-confirmed):
//   is_maker_ask=true  → maker sold → taker BOUGHT  → Buy tick
//   is_maker_ask=false → maker bid  → taker SOLD    → Sell tick
//
// Heartbeat: server sends {"type":"ping"}, client replies {"type":"pong"}.
package lighter

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

type Trades struct {
	ids *idMap
}

func NewTrades(onTick ticks.UpdateFunc) *ticks.Runner {
	return ticks.NewRunner(&Trades{ids: newIDMap()}, onTick)
}

func (a *Trades) Name() string                          { return "lighter" }
func (a *Trades) URL(_ context.Context) (string, error) { return futuresWS, nil }

func (a *Trades) BuildSubscribe(symbols []string) [][]byte {
	frames := make([][]byte, 0, len(symbols))
	ctx, cancel := context.WithTimeout(context.Background(), 6*time.Second)
	defer cancel()
	for _, s := range symbols {
		id, err := a.ids.Resolve(ctx, s)
		if err != nil {
			continue
		}
		f := map[string]any{
			"type":    "subscribe",
			"channel": "trade/" + strconv.Itoa(id),
		}
		b, _ := ticks.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

func (a *Trades) Parse(frame []byte) ([]ticks.Tick, error) {
	var msg struct {
		Type    string `json:"type"`
		Channel string `json:"channel"`
		Trades  []struct {
			TradeID    int64  `json:"trade_id"`
			TradeIDStr string `json:"trade_id_str"`
			MarketID   int    `json:"market_id"`
			Size       string `json:"size"`
			Price      string `json:"price"`
			IsMakerAsk bool   `json:"is_maker_ask"`
			Timestamp  int64  `json:"timestamp"`
			TType      string `json:"type"`
		} `json:"trades"`
	}
	if err := ticks.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	// channel comes back as "trade:<id>" (colon variant — same as
	// order_book channel echo behaviour).
	if !strings.HasPrefix(msg.Channel, "trade/") && !strings.HasPrefix(msg.Channel, "trade:") {
		return nil, nil
	}
	if len(msg.Trades) == 0 {
		return nil, nil
	}
	out := make([]ticks.Tick, 0, len(msg.Trades))
	for _, d := range msg.Trades {
		sym := a.ids.Symbol(d.MarketID)
		if sym == "" {
			continue
		}
		price, _ := strconv.ParseFloat(d.Price, 64)
		size, _ := strconv.ParseFloat(d.Size, 64)
		if price <= 0 || size <= 0 {
			continue
		}
		// is_maker_ask=true → maker was asking → taker bought → Buy
		side := ticks.Buy
		if !d.IsMakerAsk {
			side = ticks.Sell
		}
		id := d.TradeIDStr
		if id == "" {
			id = strconv.FormatInt(d.TradeID, 10)
		}
		out = append(out, ticks.Tick{
			Exchange: "lighter",
			Symbol:   sym,
			Price:    price,
			Size:     size,
			Side:     side,
			TsMS:     d.Timestamp,
			ID:       id,
		})
	}
	if len(out) == 0 {
		return nil, nil
	}
	return out, nil
}

// Lighter: server sends {"type":"ping"}, client replies {"type":"pong"}.
// Docs: client must send ≥1 frame every 2 min (app msg or pong is enough).
func (a *Trades) Heartbeat() []byte                { return nil }
func (a *Trades) HeartbeatInterval() time.Duration { return 0 }
func (a *Trades) PongFor(frame []byte) []byte {
	if strings.Contains(string(frame), `"type":"ping"`) {
		return []byte(`{"type":"pong"}`)
	}
	return nil
}
func (a *Trades) UseLibPings() bool             { return false }
func (a *Trades) SubscribeDelay() time.Duration { return 0 }
func (a *Trades) MaxSymbols() int               { return 0 }
func (a *Trades) DecompressGzip() bool          { return false }

func (a *Trades) OnReconnect() {}
