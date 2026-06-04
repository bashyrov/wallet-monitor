// Package htx — HTX (formerly Huobi) USDT-margined linear-swap.
//
// URL: wss://api.hbdm.com/linear-swap-ws
// Subscribe: {"sub":"market.<sym>-USDT.depth.size_20.high_freq","data_type":"incremental","id":"X"}
//
// QUIRKS:
//   - Frames are gzip-compressed → DecompressGzip() = true
//   - HTX sends app-level ping as JSON: {"ping": <ts>} — we reply
//     {"pong": <ts>}
package htx

import (
	"context"
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
	store *cache.Store
	books map[string]*book
	log   zerolog.Logger
}

type book struct {
	bids        map[float64]float64
	asks        map[float64]float64
	lastVersion int64 // 0 = no version seen yet; HTX version is monotonic per symbol
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{
		store: store,
		books: make(map[string]*book),
		log:   wmlog.L().With().Str("exchange", "htx").Str("layer", "depth-version").Logger(),
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
		f := map[string]any{
			"sub":       "market." + strings.ToUpper(s) + "-USDT.depth.size_20.high_freq",
			"data_type": "incremental",
			"id":        strconv.Itoa(i + 1),
		}
		b, _ := ws.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
	var msg struct {
		Ch   string `json:"ch"`
		Tick struct {
			Bids    [][]float64 `json:"bids"`
			Asks    [][]float64 `json:"asks"`
			Event   string      `json:"event"` // "snapshot" or "update"
			Version int64       `json:"version"`
		} `json:"tick"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if !strings.HasPrefix(msg.Ch, "market.") || !strings.Contains(msg.Ch, ".depth.") {
		return nil, nil
	}
	// "market.BTC-USDT.depth.size_20.high_freq" → "BTC-USDT"
	parts := strings.SplitN(msg.Ch, ".", 4)
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
	if msg.Tick.Event == "snapshot" {
		bk.bids = make(map[float64]float64)
		bk.asks = make(map[float64]float64)
		bk.lastVersion = msg.Tick.Version
	} else if msg.Tick.Event == "update" {
		// HTX version is monotonic and incremental on high_freq depth.
		// A gap means the local book has a hole — log and continue
		// (best-effort). The runner's stale-data watchdog reconnects
		// after 90s of no frames; a single missed update auto-heals on
		// next snapshot post-reconnect.
		if bk.lastVersion != 0 && msg.Tick.Version != bk.lastVersion+1 {
			a.log.Warn().
				Str("symbol", token).
				Int64("expected", bk.lastVersion+1).
				Int64("got", msg.Tick.Version).
				Int64("skipped", msg.Tick.Version-bk.lastVersion-1).
				Msg("htx version gap")
		}
		bk.lastVersion = msg.Tick.Version
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
	apply(bk.bids, msg.Tick.Bids)
	apply(bk.asks, msg.Tick.Asks)

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
