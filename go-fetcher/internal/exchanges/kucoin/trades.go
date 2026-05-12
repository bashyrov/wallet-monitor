// trades.go — KuCoin futures public trade stream.
//
// Channel: /contractMarket/execution:<TOKEN>USDTM — every fill event.
// Subscribe is one topic per frame (orderbook adapter does same). URL
// requires token+connectId via REST auth.go (shared with orderbook).
//
// Event wire (subject:"match"):
//
//	{"type":"message","topic":"/contractMarket/execution:XBTUSDTM",
//	 "subject":"match","data":{"price":"63125.5","size":100,"side":"buy",
//	   "ts":...,"tradeId":"...","makerOrderId":"...","takerOrderId":"..."}}
//
// side: "buy" / "sell" — taker side per KuCoin convention.
package kucoin

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

type Trades struct {
	auth *authClient // reuses orderbook auth
}

func NewTrades(onTick ticks.UpdateFunc) *ticks.Runner {
	return ticks.NewRunner(&Trades{auth: &authClient{}}, onTick)
}

func (a *Trades) Name() string { return "kucoin" }

func (a *Trades) URL(ctx context.Context) (string, error) {
	u, _, err := a.auth.FetchURL(ctx)
	return u, err
}

func (a *Trades) BuildSubscribe(symbols []string) [][]byte {
	frames := make([][]byte, 0, len(symbols))
	for i, s := range symbols {
		token := strings.ToUpper(s)
		if token == "BTC" {
			token = "XBT"
		}
		f := map[string]any{
			"id":             time.Now().UnixNano() + int64(i),
			"type":           "subscribe",
			"topic":          "/contractMarket/execution:" + token + "USDTM",
			"privateChannel": false,
			"response":       true,
		}
		b, _ := ticks.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

func (a *Trades) Parse(frame []byte) ([]ticks.Tick, error) {
	var msg struct {
		Type    string `json:"type"`
		Topic   string `json:"topic"`
		Subject string `json:"subject"`
		Data    struct {
			Symbol  string `json:"symbol"`
			Price   string `json:"price"`
			Size    any    `json:"size"` // sometimes int, sometimes string
			Side    string `json:"side"`
			Ts      int64  `json:"ts"`
			TradeID string `json:"tradeId"`
		} `json:"data"`
	}
	if err := ticks.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Type != "message" || !strings.HasPrefix(msg.Topic, "/contractMarket/execution:") {
		return nil, nil
	}
	contract := strings.TrimPrefix(msg.Topic, "/contractMarket/execution:")
	if !strings.HasSuffix(contract, "USDTM") {
		return nil, nil
	}
	token := strings.TrimSuffix(contract, "USDTM")
	if token == "XBT" {
		token = "BTC"
	}
	price, _ := strconv.ParseFloat(msg.Data.Price, 64)
	var size float64
	switch v := msg.Data.Size.(type) {
	case float64:
		size = v
	case string:
		size, _ = strconv.ParseFloat(v, 64)
	}
	if price <= 0 || size <= 0 {
		return nil, nil
	}
	side := ticks.Buy
	if msg.Data.Side == "sell" {
		side = ticks.Sell
	}
	// KuCoin timestamps are nanoseconds for futures execution events.
	tsMs := msg.Data.Ts
	if tsMs > 1e15 {
		tsMs = tsMs / 1e6
	}
	return []ticks.Tick{{
		Exchange: "kucoin",
		Symbol:   token,
		Price:    price,
		Size:     size,
		Side:     side,
		TsMS:     tsMs,
		ID:       msg.Data.TradeID,
	}}, nil
}

func (a *Trades) Heartbeat() []byte {
	frame, _ := ticks.MarshalJSON(map[string]any{"id": time.Now().UnixNano(), "type": "ping"})
	return frame
}
func (a *Trades) HeartbeatInterval() time.Duration { return 15 * time.Second }
func (a *Trades) PongFor(_ []byte) []byte          { return nil }
func (a *Trades) UseLibPings() bool                { return false }
func (a *Trades) SubscribeDelay() time.Duration    { return 350 * time.Millisecond }
func (a *Trades) MaxSymbols() int                  { return 30 }
func (a *Trades) DecompressGzip() bool             { return false }

func (a *Trades) OnReconnect() {}
