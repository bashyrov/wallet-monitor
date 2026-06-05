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
// BBO channel (HTX_USE_BBO=1): market.<sym>-USDT.bbo — BBO on change.
//   Subscribe: {"sub":"market.BTC-USDT.bbo","id":"sub-BTC"}
//   Inbound:   {"ch":"market.BTC-USDT.bbo","ts":N,
//               "tick":{"bid":[px,qty],"ask":[px,qty],"version":N,"ts":N}}
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

type Futures struct {
	store  *cache.Store
	books  map[string]*book
	log    zerolog.Logger
	useBBO bool // HTX_USE_BBO=1 → market.bbo; false → depth.size_20.high_freq
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
	frames := make([][]byte, 0, len(symbols))
	for i, s := range symbols {
		sym := strings.ToUpper(s) + "-USDT"
		var sub string
		if a.useBBO {
			sub = "market." + sym + ".bbo"
		} else {
			sub = "market." + sym + ".depth.size_20.high_freq"
		}
		f := map[string]any{
			"sub": sub,
			"id":  strconv.Itoa(i + 1),
		}
		if !a.useBBO {
			f["data_type"] = "incremental"
		}
		b, _ := ws.MarshalJSON(f)
		frames = append(frames, b)
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

// parseBBO handles market.<sym>-USDT.bbo frames.
// tick.bid = [price, qty], tick.ask = [price, qty]
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

	snap := &ws.Snapshot{
		Symbol: token,
		Bids:   []ws.Level{{bidPx, bidSz}},
		Asks:   []ws.Level{{askPx, askSz}},
	}
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

	return &ws.Snapshot{
		Symbol: token,
		Bids:   ws.SortedLevels(bk.bids, ws.Bids, 200),
		Asks:   ws.SortedLevels(bk.asks, ws.Asks, 200),
	}, nil
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
}
