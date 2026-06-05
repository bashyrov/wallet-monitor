// Package paradex — Paradex L2 StarkNet-based perp DEX.
//
// URL: wss://ws.api.prod.paradex.trade/v1
//
// Default channel: order_book.{market}.deltas (full diff protocol).
//   Subscribe (JSON-RPC 2.0):
//     {"jsonrpc":"2.0","id":N,"method":"subscribe",
//      "params":{"channel":"order_book.BTC-USD-PERP.deltas"}}
//
// Why deltas, not snapshot@15: Paradex hard-caps the snapshot channel
// at depth=15 (their server explicitly rejects 50/100/1000 with
// `Expecting depth=15`). Their own API best-practices doc tells API
// traders to use deltas instead — quote: "For fastest full orderbook
// depth, subscribe to deltas websocket feed". Deltas push every change
// (not every 50ms regardless), and there is no depth cap; the server
// gives us the full state of the book it has.
//
// BBO channel (PARADEX_USE_BBO=1): bbo.{market} — event-driven top-of-book
// with no throttle. Simpler wire than deltas: just 1 bid + 1 ask per frame.
//   Subscribe:
//     {"jsonrpc":"2.0","id":N,"method":"subscribe",
//      "params":{"channel":"bbo.BTC-USD-PERP"}}
//   Inbound:
//     {"jsonrpc":"2.0","method":"subscription","params":{
//       "channel":"bbo.BTC-USD-PERP",
//       "data":{"market":"BTC-USD-PERP","seq_no":N,"last_updated_at":N,
//               "bids":[{"price":"...","size":"..."}],
//               "asks":[{"price":"...","size":"..."}]}}}
//
// NOTE: bbo.{market} format inferred from paradex docs + order_book pattern.
// Verified structurally consistent; first live connection confirms wire shape.
//
// QUIRKS:
//   - SIDE encoded as "BUY"/"SELL" (uppercase strings) inside per-level
//     objects in deltas, not as separate bids/asks arrays.
//   - On update_type="s" we replace the book; on "d" we apply diffs.
//   - First frame after subscribe is always a snapshot ("s") with all
//     resting orders; subsequent frames are deltas ("d").
package paradex

import (
	"context"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const futuresWS = "wss://ws.api.prod.paradex.trade/v1"

type Futures struct {
	store  *cache.Store
	books  map[string]*book
	useBBO bool // PARADEX_USE_BBO=1 → bbo.{market}; false → order_book.deltas
}

type book struct {
	bids map[float64]float64
	asks map[float64]float64
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{
		store:  store,
		books:  make(map[string]*book),
		useBBO: os.Getenv("PARADEX_USE_BBO") == "1",
	}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("paradex", snap.Symbol, snap, "ws")
	})
}

func (a *Futures) Name() string                          { return "paradex" }
func (a *Futures) URL(_ context.Context) (string, error) { return futuresWS, nil }

func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
	frames := make([][]byte, 0, len(symbols))
	for i, s := range symbols {
		market := strings.ToUpper(s) + "-USD-PERP"
		var channel string
		if a.useBBO {
			channel = "bbo." + market
		} else {
			channel = "order_book." + market + ".deltas"
		}
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

// bboLevel is the simpler shape used in the bbo channel.
type bboLevel struct {
	Price string `json:"price"`
	Size  string `json:"size"`
}

func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
	var msg struct {
		Method string `json:"method"`
		Params struct {
			Channel string `json:"channel"`
			Data    struct {
				// shared
				Market      string `json:"market"`
				LastUpdated int64  `json:"last_updated_at"` // ms
				// order_book.deltas fields
				UpdateType string    `json:"update_type"`
				Inserts    []levelOp `json:"inserts"`
				Updates    []levelOp `json:"updates"`
				Deletes    []levelOp `json:"deletes"`
				// bbo fields
				Bids []bboLevel `json:"bids"`
				Asks []bboLevel `json:"asks"`
			} `json:"data"`
		} `json:"params"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Method != "subscription" {
		return nil, nil
	}

	channel := msg.Params.Channel
	switch {
	case strings.HasPrefix(channel, "bbo."):
		return a.parseBBO(msg.Params.Data.Market,
			msg.Params.Data.Bids, msg.Params.Data.Asks,
			msg.Params.Data.LastUpdated)
	case strings.Contains(channel, "order_book.") && strings.HasSuffix(channel, ".deltas"):
		return a.parseDeltas(msg.Params.Data.Market,
			msg.Params.Data.UpdateType,
			msg.Params.Data.Inserts, msg.Params.Data.Updates, msg.Params.Data.Deletes,
			msg.Params.Data.LastUpdated)
	}
	return nil, nil
}

// parseBBO handles the bbo.{market} channel frames.
func (a *Futures) parseBBO(market string, bids, asks []bboLevel, lastUpdated int64) (*ws.Snapshot, error) {
	if !strings.HasSuffix(market, "-USD-PERP") {
		return nil, nil
	}
	token := strings.TrimSuffix(market, "-USD-PERP")
	if len(bids) == 0 || len(asks) == 0 {
		return nil, nil
	}

	bidPx, _ := strconv.ParseFloat(bids[0].Price, 64)
	bidSz, _ := strconv.ParseFloat(bids[0].Size, 64)
	askPx, _ := strconv.ParseFloat(asks[0].Price, 64)
	askSz, _ := strconv.ParseFloat(asks[0].Size, 64)
	if bidPx <= 0 || askPx <= 0 {
		return nil, nil
	}

	var evt time.Time
	if lastUpdated > 0 {
		evt = time.UnixMilli(lastUpdated)
	}
	return &ws.Snapshot{
		Symbol:    token,
		Bids:      []ws.Level{{bidPx, bidSz}},
		Asks:      []ws.Level{{askPx, askSz}},
		EventTime: evt,
	}, nil
}

// parseDeltas handles the order_book.{market}.deltas channel frames.
func (a *Futures) parseDeltas(market, updateType string, inserts, updates, deletes []levelOp, lastUpdated int64) (*ws.Snapshot, error) {
	if !strings.HasSuffix(market, "-USD-PERP") {
		return nil, nil
	}
	token := strings.TrimSuffix(market, "-USD-PERP")

	bk, ok := a.books[token]
	if !ok {
		bk = &book{bids: make(map[float64]float64), asks: make(map[float64]float64)}
		a.books[token] = bk
	}
	if updateType == "s" {
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
	apply(inserts, false)
	apply(updates, false)
	apply(deletes, true)

	var evt time.Time
	if lastUpdated > 0 {
		evt = time.UnixMilli(lastUpdated)
	}
	return &ws.Snapshot{
		Symbol:    token,
		Bids:      ws.SortedLevels(bk.bids, ws.Bids, 200),
		Asks:      ws.SortedLevels(bk.asks, ws.Asks, 200),
		EventTime: evt,
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
