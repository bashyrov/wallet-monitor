// Package paradex — Paradex L2 StarkNet-based perp DEX.
//
// URL: wss://ws.api.prod.paradex.trade/v1
// Subscribe (JSON-RPC 2.0):
//   {"jsonrpc":"2.0","id":N,"method":"subscribe",
//    "params":{"channel":"order_book.BTC-USD-PERP.deltas"}}
//
// Why deltas, not snapshot@15: Paradex hard-caps the snapshot channel
// at depth=15 (their server explicitly rejects 50/100/1000 with
// `Expecting depth=15`). Their own API best-practices doc tells API
// traders to use deltas instead — quote: "For fastest full orderbook
// depth, subscribe to deltas websocket feed". Deltas push every change
// (not every 50ms regardless), and there is no depth cap; the server
// gives us the full state of the book it has.
//
// Inbound — DIFF protocol with three operations + snapshot/delta type:
//   {"jsonrpc":"2.0","method":"subscription","params":{
//      "channel":"order_book.BTC-USD-PERP.deltas",
//      "data":{
//        "seq_no": ..., "market": "BTC-USD-PERP", "last_updated_at": ...,
//        "update_type": "s" | "d",   // "s" snapshot, "d" delta
//        "inserts": [{"side":"BUY"|"SELL","price":"...","size":"..."}, ...],
//        "deletes": [{"side":..., "price":"..."}, ...],   // optional
//        "updates": [{"side":..., "price":"...","size":"..."}, ...] // optional
//      }
//   }}
//
// QUIRKS:
//   - SIDE encoded as "BUY"/"SELL" (uppercase strings) inside per-level
//     objects, not as separate bids/asks arrays.
//   - On update_type="s" we replace the book; on "d" we apply diffs.
//   - First frame after subscribe is always a snapshot ("s") with all
//     resting orders; subsequent frames are deltas ("d").
package paradex

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const futuresWS = "wss://ws.api.prod.paradex.trade/v1"

type Futures struct {
	store *cache.Store
	books map[string]*book
}

type book struct {
	bids map[float64]float64
	asks map[float64]float64
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{store: store, books: make(map[string]*book)}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("paradex", snap.Symbol, snap, "ws")
	})
}

func (a *Futures) Name() string                          { return "paradex" }
func (a *Futures) URL(_ context.Context) (string, error) { return futuresWS, nil }

func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
	frames := make([][]byte, 0, len(symbols))
	for i, s := range symbols {
		channel := "order_book." + strings.ToUpper(s) + "-USD-PERP.deltas"
		f := map[string]any{
			"jsonrpc": "2.0",
			"id":      i + 1,
			"method":  "subscribe",
			"params":  map[string]any{"channel": channel},
		}
		b, _ := ws.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

type levelOp struct {
	Side  string `json:"side"`
	Price string `json:"price"`
	Size  string `json:"size"`
}

func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
	var msg struct {
		Method string `json:"method"`
		Params struct {
			Channel string `json:"channel"`
			Data    struct {
				Market     string    `json:"market"`
				UpdateType string    `json:"update_type"`
				Inserts    []levelOp `json:"inserts"`
				Updates    []levelOp `json:"updates"`
				Deletes    []levelOp `json:"deletes"`
			} `json:"data"`
		} `json:"params"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Method != "subscription" {
		return nil, nil
	}
	market := msg.Params.Data.Market
	if !strings.HasSuffix(market, "-USD-PERP") {
		return nil, nil
	}
	token := strings.TrimSuffix(market, "-USD-PERP")

	bk, ok := a.books[token]
	if !ok {
		bk = &book{bids: make(map[float64]float64), asks: make(map[float64]float64)}
		a.books[token] = bk
	}
	if msg.Params.Data.UpdateType == "s" {
		bk.bids = make(map[float64]float64)
		bk.asks = make(map[float64]float64)
	}

	apply := func(ops []levelOp, removing bool) {
		for _, op := range ops {
			px, perr := strconv.ParseFloat(op.Price, 64)
			if perr != nil {
				continue
			}
			var side map[float64]float64
			switch op.Side {
			case "BUY":
				side = bk.bids
			case "SELL":
				side = bk.asks
			default:
				continue
			}
			if removing {
				delete(side, px)
				continue
			}
			sz, serr := strconv.ParseFloat(op.Size, 64)
			if serr != nil || sz <= 0 {
				delete(side, px)
				continue
			}
			side[px] = sz
		}
	}
	apply(msg.Params.Data.Inserts, false)
	apply(msg.Params.Data.Updates, false)
	apply(msg.Params.Data.Deletes, true)

	return &ws.Snapshot{
		Symbol: token,
		Bids:   ws.SortedLevels(bk.bids, ws.Bids, 200),
		Asks:   ws.SortedLevels(bk.asks, ws.Asks, 200),
	}, nil
}

func (a *Futures) Heartbeat() []byte                { return nil }
func (a *Futures) HeartbeatInterval() time.Duration { return 0 }
func (a *Futures) PongFor(_ []byte) []byte          { return nil }
func (a *Futures) UseLibPings() bool                { return true }
func (a *Futures) SubscribeDelay() time.Duration    { return 0 }
func (a *Futures) MaxSymbols() int                  { return 0 }
func (a *Futures) DecompressGzip() bool             { return false }

func (a *Futures) OnReconnect() {
	a.books = make(map[string]*book)
}
