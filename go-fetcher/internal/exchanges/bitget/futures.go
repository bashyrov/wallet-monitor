// Package bitget — Bitget V2 USDT-FUTURES + SPOT (one shared host).
//
// URL: wss://ws.bitget.com/v2/ws/public
// Subscribe (futures): {"op":"subscribe","args":[{"instType":"USDT-FUTURES","channel":"books15","instId":"BTCUSDT"}]}
// Subscribe (spot):    {"op":"subscribe","args":[{"instType":"SPOT","channel":"books15","instId":"BTCUSDT"}]}
//
// Channel "books15" pushes top-15 levels per side every ~100-200ms — the
// minimum sane depth for the arb terminal's orderbook panel. We were on
// "books1" (top-of-book only) which made the panel look stuck on 1 ask
// + 1 bid while every other venue rendered ~20 levels. "books" (full
// 200-level snapshot) is also available but heavier on bandwidth across
// 200+ subscribed symbols; books15 is the sweet spot.
//
// QUIRKS — every fix from today's prod debug session:
//   - Bug #1  (TEXT only): SendText enforced by runner
//   - Bug #4  (app-level "ping"): Heartbeat returns []byte("ping") every 25s
//   - Bug #6  (lib pings ignored): UseLibPings() returns false — proven
//                  today that lib WS-frame pings make the server silently
//                  drop the connection within 30s
//   - Bug #15 (instType differs spot/futures): two adapter types share a
//                  parser; constructor picks the value
package bitget

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const baseURL = "wss://ws.bitget.com/v2/ws/public"

// Adapter handles either futures (instType=USDT-FUTURES) or spot
// (instType=SPOT) depending on which constructor was used.
type Adapter struct {
	store    *cache.Store
	cacheKey string // "bitget" or "bitget_spot"
	instType string // "USDT-FUTURES" or "SPOT"
	books    map[string]*book
	// Phase 2g — Bitget V2 `books1` channel is a separate event-driven
	// top-of-book stream. Subscribed on the futures venue only; spot
	// keeps books15-only.
	bbo map[string]*bboLevel
}

type book struct {
	bids map[float64]float64
	asks map[float64]float64
}

type bboLevel struct {
	bidPx, bidSz float64
	askPx, askSz float64
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Adapter{
		store:    store,
		cacheKey: "bitget",
		instType: "USDT-FUTURES",
		books:    make(map[string]*book),
		bbo:      make(map[string]*bboLevel),
	}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("bitget", snap.Symbol, snap, "ws")
	})
}

func NewSpot(store *cache.Store) *ws.Runner {
	a := &Adapter{
		store:    store,
		cacheKey: "bitget_spot",
		instType: "SPOT",
		books:    make(map[string]*book),
		bbo:      make(map[string]*bboLevel),
	}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("bitget_spot", snap.Symbol, snap, "ws")
	})
}

func (a *Adapter) Name() string                          { return a.cacheKey }
func (a *Adapter) URL(_ context.Context) (string, error) { return baseURL, nil }

func (a *Adapter) BuildSubscribe(symbols []string) [][]byte {
	if len(symbols) == 0 {
		return nil
	}
	// Bitget V2: error 30002 "Unrecognized request" fires when a single
	// subscribe frame has too many args. Safe limit: 50 args per frame.
	//
	// Phase 2g bug: futures sends books15 + books1 (2 channels) per symbol.
	// Old code packed 50 symbols × 2 channels = 100 args/frame → 30002.
	// Fix: one channel per frame, 50 symbols max → 50 args/frame.
	const chunkSize = 50
	channels := []string{"books15"}
	if a.instType == "USDT-FUTURES" {
		channels = []string{"books15", "books1"}
	}
	// Estimate: len(symbols)/chunkSize chunks per channel
	est := ((len(symbols) + chunkSize - 1) / chunkSize) * len(channels)
	frames := make([][]byte, 0, est)
	for _, ch := range channels {
		for i := 0; i < len(symbols); i += chunkSize {
			end := i + chunkSize
			if end > len(symbols) {
				end = len(symbols)
			}
			args := make([]map[string]string, 0, end-i)
			for _, s := range symbols[i:end] {
				args = append(args, map[string]string{
					"instType": a.instType,
					"channel":  ch,
					"instId":   strings.ToUpper(s) + "USDT",
				})
			}
			frame := map[string]any{"op": "subscribe", "args": args}
			b, _ := ws.MarshalJSON(frame)
			frames = append(frames, b)
		}
	}
	return frames
}

func (a *Adapter) Parse(frame []byte) (*ws.Snapshot, error) {
	// Subscribe ack: {"event":"subscribe","arg":{...}}
	// Error event:   {"event":"error","msg":"...","code":...}
	// Data:          {"action":"snapshot|update","arg":{...},"data":[{"asks":[...],"bids":[...],"ts":...,"checksum":...}]}
	var msg struct {
		Event  string `json:"event"`
		Action string `json:"action"`
		Arg    struct {
			InstType string `json:"instType"`
			Channel  string `json:"channel"`
			InstID   string `json:"instId"`
		} `json:"arg"`
		Data []struct {
			Bids [][]string `json:"bids"`
			Asks [][]string `json:"asks"`
		} `json:"data"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Event != "" {
		return nil, nil
	}
	isDepth := msg.Arg.Channel == "books15"
	isBBO := msg.Arg.Channel == "books1"
	if !isDepth && !isBBO {
		return nil, nil
	}
	if msg.Arg.InstType != a.instType {
		// Wrong leg — futures adapter shouldn't process spot data even if
		// the same connection ever multiplexed (it doesn't, but defensive).
		return nil, nil
	}
	if !strings.HasSuffix(msg.Arg.InstID, "USDT") {
		return nil, nil
	}
	token := strings.TrimSuffix(msg.Arg.InstID, "USDT")
	if len(msg.Data) == 0 {
		return nil, nil
	}
	d := msg.Data[0]

	if isBBO {
		return a.applyBBO(token, d.Bids, d.Asks), nil
	}

	bk, ok := a.books[token]
	if !ok {
		bk = &book{bids: make(map[float64]float64), asks: make(map[float64]float64)}
		a.books[token] = bk
	}
	if msg.Action == "snapshot" {
		bk.bids = make(map[float64]float64, len(d.Bids))
		bk.asks = make(map[float64]float64, len(d.Asks))
	}
	apply := func(side map[float64]float64, rows [][]string) {
		for _, r := range rows {
			if len(r) < 2 {
				continue
			}
			px, _ := strconv.ParseFloat(r[0], 64)
			sz, _ := strconv.ParseFloat(r[1], 64)
			if sz == 0 {
				delete(side, px)
			} else {
				side[px] = sz
			}
		}
	}
	apply(bk.bids, d.Bids)
	apply(bk.asks, d.Asks)
	return a.mergedSnapshot(token), nil
}

func (a *Adapter) applyBBO(token string, bidRows, askRows [][]string) *ws.Snapshot {
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
			b.bidPx, b.bidSz = 0, 0
		} else {
			b.bidPx, b.bidSz = px, sz
		}
	} else {
		b.bidPx, b.bidSz = 0, 0
	}
	if px, sz, ok := parseLvl(askRows); ok {
		if sz == 0 {
			b.askPx, b.askSz = 0, 0
		} else {
			b.askPx, b.askSz = px, sz
		}
	} else {
		b.askPx, b.askSz = 0, 0
	}
	return a.mergedSnapshot(token)
}

func (a *Adapter) mergedSnapshot(token string) *ws.Snapshot {
	bk := a.books[token]
	var bids, asks []ws.Level
	if bk != nil {
		bids = ws.SortedLevels(bk.bids, ws.Bids, 200)
		asks = ws.SortedLevels(bk.asks, ws.Asks, 200)
	}
	b := a.bbo[token]
	if b == nil || b.bidPx <= 0 || b.askPx <= 0 || b.bidPx >= b.askPx {
		// BBO absent or self-crossed (shouldn't happen) — depth only.
		return &ws.Snapshot{Symbol: token, Bids: bids, Asks: asks}
	}
	// BBO is internally consistent (bid < ask). Before splicing, purge stale
	// depth levels that contradict the BBO:
	//   - depth asks at or below BBO bid are filled (BBO says so) → remove
	//   - depth bids at or above BBO ask are filled → remove
	// This prevents a crossed book when books1 fires faster than books15.
	cleaned := bids[:0]
	for _, lvl := range bids {
		if lvl[0] < b.askPx {
			cleaned = append(cleaned, lvl)
		}
	}
	bids = cleaned

	cleanedA := asks[:0]
	for _, lvl := range asks {
		if lvl[0] > b.bidPx {
			cleanedA = append(cleanedA, lvl)
		}
	}
	asks = cleanedA

	bids = spliceBBOBid(bids, b.bidPx, b.bidSz)
	asks = spliceBBOAsk(asks, b.askPx, b.askSz)
	return &ws.Snapshot{Symbol: token, Bids: bids, Asks: asks}
}

// spliceBBOBid / spliceBBOAsk — same semantics as bybit/okx:
// BBO at strictly better price prepends; same price refreshes size;
// worse no-ops; zero BBO no-ops.
func spliceBBOBid(bids []ws.Level, bboPx, bboSz float64) []ws.Level {
	if bboPx <= 0 {
		return bids
	}
	if len(bids) == 0 {
		return []ws.Level{{bboPx, bboSz}}
	}
	if bboPx > bids[0][0] {
		return append([]ws.Level{{bboPx, bboSz}}, bids...)
	}
	if bboPx == bids[0][0] {
		bids[0][1] = bboSz
	}
	return bids
}

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

// Heartbeat — Bug #4 from today: Bitget V2 needs literal "ping" text frame
// every <30s. Server ignores lib pings (Bug #6). 25s gives margin.
func (a *Adapter) Heartbeat() []byte                { return []byte("ping") }
func (a *Adapter) HeartbeatInterval() time.Duration { return 25 * time.Second }

// Server replies with literal "pong" — runner consumes via the lowercase
// "ping"/"pong" path before reaching adapter Parse(). Nothing to do here.
func (a *Adapter) PongFor(_ []byte) []byte       { return nil }
func (a *Adapter) UseLibPings() bool              { return false } // bug #6
func (a *Adapter) SubscribeDelay() time.Duration { return 200 * time.Millisecond }
func (a *Adapter) MaxSymbols() int                { return 0 }
func (a *Adapter) DecompressGzip() bool           { return false }

// BuildUnsubscribe implements ws.Unsubscriber. Bitget V2: op:unsubscribe with
// args per channel (books15, books1 for futures). One frame per channel to
// stay within 50-args/frame limit. Clears local state.
func (a *Adapter) BuildUnsubscribe(symbols []string) [][]byte {
	channels := []string{"books15"}
	if a.instType == "USDT-FUTURES" {
		channels = []string{"books15", "books1"}
	}
	frames := make([][]byte, 0, len(channels))
	for _, ch := range channels {
		args := make([]map[string]string, 0, len(symbols))
		for _, s := range symbols {
			args = append(args, map[string]string{
				"instType": a.instType,
				"channel":  ch,
				"instId":   strings.ToUpper(s) + "USDT",
			})
		}
		b, _ := ws.MarshalJSON(map[string]any{"op": "unsubscribe", "args": args})
		frames = append(frames, b)
	}
	// Clear local state for removed symbols.
	for _, s := range symbols {
		token := strings.ToUpper(s)
		delete(a.books, token)
		delete(a.bbo, token)
	}
	return frames
}

func (a *Adapter) OnReconnect() {
	a.books = make(map[string]*book)
	a.bbo = make(map[string]*bboLevel)
}
