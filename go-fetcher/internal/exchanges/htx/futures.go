// Package htx — HTX (formerly Huobi) USDT-margined linear-swap.
//
// URL: wss://api.hbdm.com/linear-swap-ws
//
// Default channel: depth.size_20.high_freq (incremental, event-driven).
//   Subscribe: {"sub":"market.<sym>-USDT.depth.size_20.high_freq","data_type":"incremental","id":"X"}
//   Inbound:   {"ch":"market.<sym>-USDT.depth.size_20.high_freq",
//               "tick":{"event":"snapshot"|"update","version":N,
//                        "bids":[[px,sz],...],"asks":[[px,sz],...]}}
//
// BBO channel (HTX_USE_BBO=1): hybrid dual-track:
//   - depth.size_20.high_freq subscribed → feeds books[token] (depth state)
//   - market.<sym>-USDT.bbo   subscribed → feeds bbo[token]  (BBO overlay)
//   mergedSnapshot splices BBO over depth top → full ladder + fast BBO.
//
// QUIRKS:
//   - Frames are gzip-compressed → DecompressGzip() = true
//   - HTX sends app-level ping as JSON: {"ping": <ts>} — we reply
//     {"pong": <ts>}
package htx

import (
	"context"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/rs/zerolog"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	wmlog "github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const futuresWS = "wss://api.hbdm.com/linear-swap-ws"

type bboLevel struct {
	bidPx, bidSz float64
	askPx, askSz float64
}

type Futures struct {
	store  *cache.Store
	books  map[string]*book
	bbo    map[string]*bboLevel
	log    zerolog.Logger
	useBBO bool // HTX_USE_BBO=1 → dual-track (depth + BBO); false → depth only
}

type book struct {
	bids        map[float64]float64
	asks        map[float64]float64
	lastVersion int64 // 0 = no version seen yet; HTX version is monotonic per symbol
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{
		store:  store,
		books:  make(map[string]*book),
		bbo:    make(map[string]*bboLevel),
		log:    wmlog.L().With().Str("exchange", "htx").Str("layer", "depth-version").Logger(),
		useBBO: os.Getenv("HTX_USE_BBO") == "1",
	}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("htx", snap.Symbol, snap, "ws")
	})
}

func (a *Futures) Name() string                          { return "htx" }
func (a *Futures) URL(_ context.Context) (string, error) { return futuresWS, nil }

func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
	// Dual-track when HTX_USE_BBO=1: subscribe to BOTH depth AND BBO channels.
	// One frame per symbol per channel.
	frames := make([][]byte, 0, len(symbols)*2)
	for i, s := range symbols {
		sym := strings.ToUpper(s) + "-USDT"
		// Always subscribe to depth for the full ladder.
		depthSub := "market." + sym + ".depth.size_20.high_freq"
		df := map[string]any{"sub": depthSub, "id": strconv.Itoa(i+1), "data_type": "incremental"}
		db, _ := ws.MarshalJSON(df)
		frames = append(frames, db)
		// Also subscribe to BBO when flag is set.
		if a.useBBO {
			bboSub := "market." + sym + ".bbo"
			bf := map[string]any{"sub": bboSub, "id": strconv.Itoa(len(symbols) + i + 1)}
			bb, _ := ws.MarshalJSON(bf)
			frames = append(frames, bb)
		}
	}
	return frames
}

func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
	var msg struct {
		Ch   string `json:"ch"`
		Ts   int64  `json:"ts"` // envelope timestamp ms
		Tick struct {
			// depth fields
			Bids    [][]float64 `json:"bids"`
			Asks    [][]float64 `json:"asks"`
			Event   string      `json:"event"` // "snapshot" or "update"
			Version int64       `json:"version"`
			// bbo fields — 2-element [price, qty] arrays
			Bid []float64 `json:"bid"` // [bidPx, bidQty]
			Ask []float64 `json:"ask"` // [askPx, askQty]
		} `json:"tick"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}

	ch := msg.Ch
	if !strings.HasPrefix(ch, "market.") {
		return nil, nil
	}

	if strings.Contains(ch, ".bbo") {
		return a.parseBBO(ch, msg.Tick.Bid, msg.Tick.Ask, msg.Ts)
	}
	if strings.Contains(ch, ".depth.") {
		return a.parseDepth(ch, msg.Tick.Bids, msg.Tick.Asks, msg.Tick.Event, msg.Tick.Version)
	}
	return nil, nil
}

// parseBBO handles market.<sym>-USDT.bbo frames — updates bbo state and emits
// a merged snapshot (depth + BBO overlay).
func (a *Futures) parseBBO(ch string, bid, ask []float64, ts int64) (*ws.Snapshot, error) {
	// ch = "market.BTC-USDT.bbo" → extract "BTC-USDT"
	parts := strings.SplitN(ch, ".", 4)
	if len(parts) < 3 {
		return nil, nil
	}
	pair := parts[1]
	if !strings.HasSuffix(pair, "-USDT") {
		return nil, nil
	}
	token := strings.TrimSuffix(pair, "-USDT")

	if len(bid) < 2 || len(ask) < 2 {
		return nil, nil
	}
	bidPx, bidSz := bid[0], bid[1]
	askPx, askSz := ask[0], ask[1]
	if bidPx <= 0 || askPx <= 0 {
		return nil, nil
	}

	b, ok := a.bbo[token]
	if !ok {
		b = &bboLevel{}
		a.bbo[token] = b
	}
	b.bidPx, b.bidSz = bidPx, bidSz
	b.askPx, b.askSz = askPx, askSz

	snap := a.mergedSnapshot(token)
	if ts > 0 {
		snap.EventTime = time.UnixMilli(ts)
	}
	return snap, nil
}

// parseDepth handles depth.size_20.high_freq incremental frames.
func (a *Futures) parseDepth(ch string, bids, asks [][]float64, event string, version int64) (*ws.Snapshot, error) {
	// ch = "market.BTC-USDT.depth.size_20.high_freq"
	parts := strings.SplitN(ch, ".", 4)
	if len(parts) < 2 {
		return nil, nil
	}
	pair := parts[1]
	if !strings.HasSuffix(pair, "-USDT") {
		return nil, nil
	}
	token := strings.TrimSuffix(pair, "-USDT")

	bk, ok := a.books[token]
	if !ok {
		bk = &book{bids: make(map[float64]float64), asks: make(map[float64]float64)}
		a.books[token] = bk
	}
	if event == "snapshot" {
		bk.bids = make(map[float64]float64)
		bk.asks = make(map[float64]float64)
		bk.lastVersion = version
	} else if event == "update" {
		if bk.lastVersion != 0 && version != bk.lastVersion+1 {
			a.log.Warn().
				Str("symbol", token).
				Int64("expected", bk.lastVersion+1).
				Int64("got", version).
				Int64("skipped", version-bk.lastVersion-1).
				Msg("htx version gap")
		}
		bk.lastVersion = version
	}
	apply := func(side map[float64]float64, rows [][]float64) {
		for _, r := range rows {
			if len(r) < 2 {
				continue
			}
			if r[1] == 0 {
				delete(side, r[0])
			} else {
				side[r[0]] = r[1]
			}
		}
	}
	apply(bk.bids, bids)
	apply(bk.asks, asks)

	return a.mergedSnapshot(token), nil
}

// mergedSnapshot — depth state with BBO overlay, stale depth purged on BBO boundaries.
func (a *Futures) mergedSnapshot(token string) *ws.Snapshot {
	bk := a.books[token]
	var bids, asks []ws.Level
	if bk != nil {
		bids = ws.SortedLevels(bk.bids, ws.Bids, 200)
		asks = ws.SortedLevels(bk.asks, ws.Asks, 200)
	}
	b := a.bbo[token]
	if b == nil || b.bidPx <= 0 || b.askPx <= 0 || b.bidPx >= b.askPx {
		return &ws.Snapshot{Symbol: token, Bids: bids, Asks: asks}
	}
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

// HTX sends {"ping": N} every 5s — we reply {"pong": N}. Sonic preserves
// number type round-trip; using a string here triggers a server kick.
func (a *Futures) PongFor(frame []byte) []byte {
	var msg struct {
		Ping int64 `json:"ping"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil
	}
	if msg.Ping == 0 {
		return nil
	}
	reply, _ := ws.MarshalJSON(map[string]int64{"pong": msg.Ping})
	return reply
}

func (a *Futures) Heartbeat() []byte                { return nil }
func (a *Futures) HeartbeatInterval() time.Duration { return 0 }
func (a *Futures) UseLibPings() bool                { return false }
func (a *Futures) SubscribeDelay() time.Duration    { return 0 }
func (a *Futures) MaxSymbols() int                  { return 0 }
func (a *Futures) DecompressGzip() bool             { return true }

func (a *Futures) OnReconnect() {
	a.books = make(map[string]*book)
	a.bbo = make(map[string]*bboLevel)
}
