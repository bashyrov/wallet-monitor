// trades.go — Paradex public trade stream.
//
// Same WS endpoint + JSON-RPC pattern as the orderbook adapter.
// Channel: trades.<MKT>-USD-PERP — pushed per fill, public (no JWT).
//
// Subscribe:
//
//	{"jsonrpc":"2.0","id":N,"method":"subscribe",
//	 "params":{"channel":"trades.BTC-USD-PERP"}}
//
// Event push (JSON-RPC subscription notification):
//
//	{"jsonrpc":"2.0","method":"subscription",
//	 "params":{"channel":"trades.BTC-USD-PERP",
//	  "data":{"created_at":1672531200000,"id":"trade123","market":"BTC-USD-PERP",
//	          "price":"42000.50","side":"BUY","size":"1.5","trade_type":"FILL"}}}
//
// side: "BUY" | "SELL" — taker side.
// trade_type: FILL | LIQUIDATION | TRANSFER | SETTLE_MARKET | RPI | BLOCK_TRADE
// We accept FILL + LIQUIDATION (real price-moving fills), ignore the rest.
package paradex

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

const tradesWS = "wss://ws.api.prod.paradex.trade/v1"

type Trades struct{}

func NewTrades(onTick ticks.UpdateFunc) *ticks.Runner {
	return ticks.NewRunner(&Trades{}, onTick)
}

func (a *Trades) Name() string                          { return "paradex" }
func (a *Trades) URL(_ context.Context) (string, error) { return tradesWS, nil }

func (a *Trades) BuildSubscribe(symbols []string) [][]byte {
	frames := make([][]byte, 0, len(symbols))
	for i, s := range symbols {
		channel := "trades." + strings.ToUpper(s) + "-USD-PERP"
		f := map[string]any{
			"jsonrpc": "2.0",
			"id":      i + 1,
			"method":  "subscribe",
			"params":  map[string]any{"channel": channel},
		}
		b, _ := ticks.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

func (a *Trades) Parse(frame []byte) ([]ticks.Tick, error) {
	var msg struct {
		Method string `json:"method"`
		Params struct {
			Channel string `json:"channel"`
			Data    struct {
				CreatedAt int64  `json:"created_at"`
				ID        string `json:"id"`
				Market    string `json:"market"`
				Price     string `json:"price"`
				Side      string `json:"side"`
				Size      string `json:"size"`
				TradeType string `json:"trade_type"`
			} `json:"data"`
		} `json:"params"`
	}
	if err := ticks.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Method != "subscription" || !strings.HasPrefix(msg.Params.Channel, "trades.") {
		return nil, nil
	}
	d := msg.Params.Data
	if !strings.HasSuffix(d.Market, "-USD-PERP") {
		return nil, nil
	}
	// Only FILL + LIQUIDATION carry price-moving info; skip transfers/settlements.
	if d.TradeType != "" && d.TradeType != "FILL" && d.TradeType != "LIQUIDATION" {
		return nil, nil
	}
	token := strings.TrimSuffix(d.Market, "-USD-PERP")
	price, _ := strconv.ParseFloat(d.Price, 64)
	size, _ := strconv.ParseFloat(d.Size, 64)
	if price <= 0 || size <= 0 {
		return nil, nil
	}
	side := ticks.Buy
	if d.Side == "SELL" {
		side = ticks.Sell
	}
	return []ticks.Tick{{
		Exchange: "paradex",
		Symbol:   token,
		Price:    price,
		Size:     size,
		Side:     side,
		TsMS:     d.CreatedAt,
		ID:       d.ID,
	}}, nil
}

// Paradex server pings every 55s; lib pong reply handles it.
func (a *Trades) Heartbeat() []byte                { return nil }
func (a *Trades) HeartbeatInterval() time.Duration { return 0 }
func (a *Trades) PongFor(_ []byte) []byte          { return nil }
func (a *Trades) UseLibPings() bool                { return true }
func (a *Trades) SubscribeDelay() time.Duration    { return 0 }
func (a *Trades) MaxSymbols() int                  { return 0 }
func (a *Trades) DecompressGzip() bool             { return false }

func (a *Trades) OnReconnect() {}
