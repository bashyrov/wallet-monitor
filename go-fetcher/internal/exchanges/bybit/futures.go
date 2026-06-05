// Package bybit implements the Bybit V5 perp orderbook WS.
//
// Two channels for the same symbol:
//   - orderbook.50.{symbol}USDT — 50-level snapshot+delta @ ~20ms cadence
//   - orderbook.1.{symbol}USDT  — top-of-book event-driven @ ~10ms
//     (Phase 2b — added so the arb engine sees BBO updates between
//      depth pushes; net: top-of-book latency p50 drops ~10ms on
//      hot pairs vs depth-only)
//
// Wire format identical between the two channels: `{topic, type, data}`
// with type=snapshot|delta. We feed them into SEPARATE state stores:
//
//   book.bids/asks      — full 50-level state, fed by orderbook.50
//   bbo.bidPx/bidSz/...  — single top, fed by orderbook.1
//
// On emit (either channel triggers): produce a Snapshot from the 50-level
// state with BBO spliced over the top when BBO has a better price (newer
// information). BBO never DELETES depth levels — depth deltas handle that
// independently. This keeps the two streams from contaminating each
// other's state.
//
// URL: wss://stream.bybit.com/v5/public/linear
//
// Bug-resistance:
//   - Bug #1  (TEXT frame)        : runner.SendText only
//   - Bug #2  (policy 1008)       : runner backoff (Bybit not historically prone)
//   - Bug #7  (volume on partial) : DELTAS carry size only on changed levels;
//                                   our merge preserves untouched sizes.
//   - Bug #20 (stale TCP)         : runner watchdog
package bybit

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const futuresWS = "wss://stream.bybit.com/v5/public/linear"

type Futures struct {
	store *cache.Store
	// Per-symbol 50-level price→size for both sides. Bybit V5 sends
	// snapshots on connect + deltas after; we maintain the merged book.
	books map[string]*book
	// Per-symbol top-of-book from orderbook.1 — separate state so a
	// snapshot frame on orderbook.1 doesn't wipe the 50-level state.
	bbo map[string]*bboLevel
}

type book struct {
	bids map[float64]float64
	asks map[float64]float64
}

// bboLevel — single top-of-book from orderbook.1.
// Zero values for bidPx/askPx mean "no BBO update yet".
type bboLevel struct {
	bidPx, bidSz float64
	askPx, askSz float64
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{
		store: store,
		books: make(map[string]*book),
		bbo:   make(map[string]*bboLevel),
	}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("bybit", snap.Symbol, snap, "ws")
	})
}

func (a *Futures) Name() string                          { return "bybit" }
func (a *Futures) URL(_ context.Context) (string, error) { return futuresWS, nil }

func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
	// Bybit rejects the WHOLE subscribe payload if even one topic is
	// invalid — symptom: "error:handler not found,topic:orderbook.50.X".
	// Bootstrap pulls top-N from Python's funding.json which can include
	// symbols that exist on Binance/Gate/etc but not on Bybit (e.g.
	// SPYX). Send one args list per topic so a single bad symbol just
	// fails alone and the rest still subscribe.
	//
	// Two subscribes per symbol — depth (orderbook.50) + BBO (orderbook.1).
	frames := make([][]byte, 0, 2*len(symbols))
	for _, s := range symbols {
		sym := strings.ToUpper(s) + "USDT"
		for _, topic := range []string{"orderbook.50." + sym, "orderbook.1." + sym} {
			frame := map[string]any{
				"op":   "subscribe",
				"args": []string{topic},
			}
			b, _ := ws.MarshalJSON(frame)
			frames = append(frames, b)
		}
	}
	return frames
}

func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
	// Bybit uses three top-level shapes: subscribe ack {success, op, ...},
	// pong {op:pong}, data {topic:"orderbook.50.X"|"orderbook.1.X",
	// type:"snapshot|delta", data:{...}}.
	var msg struct {
		Topic string `json:"topic"`
		Type  string `json:"type"`
		Data  struct {
			Symbol string     `json:"s"`
			Bids   [][]string `json:"b"`
			Asks   [][]string `json:"a"`
		} `json:"data"`
		Op  string `json:"op"`
		Ret string `json:"retMsg"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Op != "" || msg.Ret != "" {
		// subscribe ack / pong — not data
		return nil, nil
	}
	isDepth := strings.HasPrefix(msg.Topic, "orderbook.50.")
	isBBO := strings.HasPrefix(msg.Topic, "orderbook.1.")
	if !isDepth && !isBBO {
		return nil, nil
	}
	sym := msg.Data.Symbol
	if !strings.HasSuffix(sym, "USDT") {
		return nil, nil
	}
	token := strings.TrimSuffix(sym, "USDT")

	if isBBO {
		return a.applyBBO(token, msg.Type, msg.Data.Bids, msg.Data.Asks), nil
	}
	return a.applyDepth(token, msg.Type, msg.Data.Bids, msg.Data.Asks), nil
}

// applyDepth — update the 50-level state, emit a merged Snapshot.
func (a *Futures) applyDepth(token, typ string, bidRows, askRows [][]string) *ws.Snapshot {
	bk, ok := a.books[token]
	if !ok {
		bk = &book{bids: make(map[float64]float64), asks: make(map[float64]float64)}
		a.books[token] = bk
	}
	if typ == "snapshot" {
		bk.bids = make(map[float64]float64, len(bidRows))
		bk.asks = make(map[float64]float64, len(askRows))
	}
	apply := func(side map[float64]float64, rows [][]string) {
		for _, r := range rows {
			if len(r) < 2 {
				continue
			}
			px, perr := strconv.ParseFloat(r[0], 64)
			sz, serr := strconv.ParseFloat(r[1], 64)
			if perr != nil || serr != nil {
				continue
			}
			if sz == 0 {
				delete(side, px)
			} else {
				side[px] = sz
			}
		}
	}
	apply(bk.bids, bidRows)
	apply(bk.asks, askRows)
	return a.mergedSnapshot(token)
}

// applyBBO — update the top-of-book state from orderbook.1.
// orderbook.1 sends at most one bid + one ask per frame. snapshot replaces;
// delta updates the side(s) that changed.
func (a *Futures) applyBBO(token, typ string, bidRows, askRows [][]string) *ws.Snapshot {
	b, ok := a.bbo[token]
	if !ok {
		b = &bboLevel{}
		a.bbo[token] = b
	}
	parseLvl := func(rows [][]string) (px, sz float64, ok bool) {
		if len(rows) == 0 || len(rows[0]) < 2 {
			return 0, 0, false
		}
		px, perr := strconv.ParseFloat(rows[0][0], 64)
		sz, serr := strconv.ParseFloat(rows[0][1], 64)
		if perr != nil || serr != nil {
			return 0, 0, false
		}
		return px, sz, true
	}
	if px, sz, ok := parseLvl(bidRows); ok {
		if sz == 0 {
			// BBO size=0 means top bid evaporated — clear our cached BBO bid.
			b.bidPx, b.bidSz = 0, 0
		} else {
			b.bidPx, b.bidSz = px, sz
		}
	} else if typ == "snapshot" {
		// snapshot with no bid → no bid side at all
		b.bidPx, b.bidSz = 0, 0
	}
	if px, sz, ok := parseLvl(askRows); ok {
		if sz == 0 {
			b.askPx, b.askSz = 0, 0
		} else {
			b.askPx, b.askSz = px, sz
		}
	} else if typ == "snapshot" {
		b.askPx, b.askSz = 0, 0
	}
	return a.mergedSnapshot(token)
}

// mergedSnapshot — produce the emit-time Snapshot: depth state from
// orderbook.50, with BBO spliced over the top when BBO is strictly
// better (higher bid / lower ask) or refreshes the size at the same
// price level. If BBO is worse or stale (zero), depth state is used
// as-is.
func (a *Futures) mergedSnapshot(token string) *ws.Snapshot {
	bk := a.books[token]
	var bids, asks []ws.Level
	if bk != nil {
		bids = ws.SortedLevels(bk.bids, ws.Bids, 200)
		asks = ws.SortedLevels(bk.asks, ws.Asks, 200)
	}
	if b := a.bbo[token]; b != nil {
		bids = spliceBBOBid(bids, b.bidPx, b.bidSz)
		asks = spliceBBOAsk(asks, b.askPx, b.askSz)
	}
	return &ws.Snapshot{Symbol: token, Bids: bids, Asks: asks}
}

// spliceBBOBid — insert/refresh BBO bid in the descending-price bid
// slice. BBO better (higher px) than current top → prepend. Same price
// → refresh size. Worse → leave depth as-is.
func spliceBBOBid(bids []ws.Level, bboPx, bboSz float64) []ws.Level {
	if bboPx <= 0 {
		return bids
	}
	if len(bids) == 0 {
		return []ws.Level{{bboPx, bboSz}}
	}
	if bboPx > bids[0][0] {
		// strictly better — prepend
		return append([]ws.Level{{bboPx, bboSz}}, bids...)
	}
	if bboPx == bids[0][0] {
		// same level — refresh size
		bids[0][1] = bboSz
	}
	return bids
}

// spliceBBOAsk — symmetric for asks (ascending price, lower = better).
func spliceBBOAsk(asks []ws.Level, bboPx, bboSz float64) []ws.Level {
	if bboPx <= 0 {
		return asks
	}
	if len(asks) == 0 {
		return []ws.Level{{bboPx, bboSz}}
	}
	if bboPx < asks[0][0] {
		return append([]ws.Level{{bboPx, bboSz}}, asks...)
	}
	if bboPx == asks[0][0] {
		asks[0][1] = bboSz
	}
	return asks
}

// Bybit V5 keepalive: client sends {"op":"ping"} every <30s. Server
// replies {"op":"pong","success":true,...}. Bybit DOES NOT send
// unsolicited pings to clients on public streams — we don't reply to
// anything. (Replying with "op":"pong" got the connection error
// "invalid op" — bybit treats client-sent pong as malformed.)
func (a *Futures) Heartbeat() []byte                { return []byte(`{"op":"ping"}`) }
func (a *Futures) HeartbeatInterval() time.Duration { return 20 * time.Second }
func (a *Futures) PongFor(_ []byte) []byte          { return nil }
func (a *Futures) UseLibPings() bool                { return false }
func (a *Futures) SubscribeDelay() time.Duration    { return 0 }
func (a *Futures) MaxSymbols() int                  { return 0 }
func (a *Futures) DecompressGzip() bool             { return false }

// BuildUnsubscribe implements ws.Unsubscriber. Sends op:unsubscribe for both
// orderbook.50 and orderbook.1 per removed symbol. Clears local state.
func (a *Futures) BuildUnsubscribe(symbols []string) [][]byte {
	frames := make([][]byte, 0, len(symbols))
	for _, s := range symbols {
		sym := strings.ToUpper(s) + "USDT"
		args := []string{"orderbook.50." + sym, "orderbook.1." + sym}
		frame := map[string]any{"op": "unsubscribe", "args": args}
		b, _ := ws.MarshalJSON(frame)
		frames = append(frames, b)
		// Clear local state for this symbol.
		token := strings.ToUpper(s)
		delete(a.books, token)
		delete(a.bbo, token)
	}
	return frames
}

// OnReconnect — clear both local stores so the next snapshots seed cleanly.
func (a *Futures) OnReconnect() {
	a.books = make(map[string]*book)
	a.bbo = make(map[string]*bboLevel)
}
