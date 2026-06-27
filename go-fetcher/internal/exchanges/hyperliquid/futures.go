// Package hyperliquid — Hyperliquid L1 perp DEX, WS orderbook + REST backstop.
//
// URL: wss://api.hyperliquid.xyz/ws
//
// Channel: l2Book — full snapshot per block update, ≥500ms cadence.
//   Subscribe: {"method":"subscribe","subscription":{"type":"l2Book","coin":"BTC"}}
//   Inbound:   {"channel":"l2Book","data":{"coin":"BTC","time":N,
//               "levels":[[{px,sz,n},...],[{px,sz,n},...]]}}
//
// HL_USE_BBO=1 is a no-op: l2Book already gives full depth AND the same
// per-block cadence as the bbo channel. Dual-track (l2Book+bbo) was tried
// but caused a livelock: 20 sym × 2 × 500ms = 20s subscribe, but the 5s
// reconcile cycle forced reconnect before completion → 0/s in prod.
//
// Cadence note: HL pushes l2Book only when the orderbook actually changes
// inside a block (~1Hz max). For active coins (BTC/ETH) this gives
// 0.2-0.5 updates/s in prod; for low-volume coins like GRASS it drops to
// ~0.1/s. Compared to Binance/Bybit's 100ms snapshot cadence (10/s) this
// felt visibly stale on /arb, hence the REST backstop below.
//
// QUIRKS:
//   - levels[0] = bids, levels[1] = asks
//   - Each level is an OBJECT with px/sz/n, NOT an array
//   - HL drops connection with "write: broken pipe" after 4-8 subscribe
//     frames — 500ms SubscribeDelay is required
package hyperliquid

import (
	"bytes"
	"context"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bytedance/sonic"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const (
	futuresWS   = "wss://api.hyperliquid.xyz/ws"
	infoREST    = "https://api.hyperliquid.xyz/info"
	backstopGap = 1500 * time.Millisecond // refetch if cache older than this
	backstopMax = 8                       // max parallel REST fetches per tick
)

type Futures struct {
	store  *cache.Store
	useBBO bool // kept for config compatibility; currently a no-op

	// symMu guards `symbols` — populated on every BuildSubscribe so the
	// REST backstop knows which coins to refresh.
	symMu   sync.RWMutex
	symbols map[string]struct{}

	httpc *http.Client
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{
		store:   store,
		useBBO:  os.Getenv("HL_USE_BBO") == "1",
		symbols: make(map[string]struct{}, 64),
		httpc:   &http.Client{Timeout: 3 * time.Second},
	}
	runner := ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("hyperliquid", snap.Symbol, snap, "ws")
	})
	// Background REST backstop — runs for process lifetime. HL's l2Book
	// WS only pushes on block change, so for low-vol coins (GRASS et al.)
	// updates can be 5-10s apart. The backstop guarantees a 1.5s floor.
	go a.restBackstop(context.Background())
	return runner
}

func (a *Futures) Name() string                          { return "hyperliquid" }
func (a *Futures) URL(_ context.Context) (string, error) { return futuresWS, nil }

func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
	// Track the subscribed set so the REST backstop knows which coins to
	// refresh between block updates. Called every reconcile cycle.
	a.symMu.Lock()
	a.symbols = make(map[string]struct{}, len(symbols))
	for _, s := range symbols {
		a.symbols[strings.ToUpper(s)] = struct{}{}
	}
	a.symMu.Unlock()

	// l2Book only: full depth + block cadence. Dual-track caused livelock.
	frames := make([][]byte, 0, len(symbols))
	for _, s := range symbols {
		f := map[string]any{
			"method":       "subscribe",
			"subscription": map[string]any{"type": "l2Book", "coin": strings.ToUpper(s)},
		}
		b, _ := ws.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

// hlLevel matches l2Book level objects.
type hlLevel struct {
	Px string `json:"px"`
	Sz string `json:"sz"`
	N  int    `json:"n"`
}

func parseHLLevel(r hlLevel) (px, sz float64) {
	px, _ = strconv.ParseFloat(r.Px, 64)
	sz, _ = strconv.ParseFloat(r.Sz, 64)
	return
}

func parseHLSide(rows []hlLevel) []ws.Level {
	out := make([]ws.Level, 0, len(rows))
	for _, r := range rows {
		px, sz := parseHLLevel(r)
		if sz > 0 {
			out = append(out, ws.Level{px, sz})
		}
	}
	return out
}

func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
	var msg struct {
		Channel string `json:"channel"`
		Data    struct {
			Coin   string       `json:"coin"`
			Time   int64        `json:"time"`
			Levels [2][]hlLevel `json:"levels"` // [bids, asks]
		} `json:"data"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}

	if msg.Channel != "l2Book" {
		return nil, nil
	}

	var evt time.Time
	if msg.Data.Time > 0 {
		evt = time.UnixMilli(msg.Data.Time)
	}
	return &ws.Snapshot{
		Symbol:    strings.ToUpper(msg.Data.Coin),
		Bids:      parseHLSide(msg.Data.Levels[0]),
		Asks:      parseHLSide(msg.Data.Levels[1]),
		EventTime: evt,
	}, nil
}

// ── REST backstop ──────────────────────────────────────────────────────
//
// Ticks every 1s. For each subscribed symbol whose cached entry is older
// than backstopGap (1.5s), kicks off a goroutine that POSTs /info l2Book
// and writes the snapshot into the same Store the WS path uses.
// Concurrency is bounded to backstopMax (8) parallel requests via a
// buffered chan semaphore so we don't burst HL's rate limit (1200/min =
// 20/s sustained, l2Book is weight 1). At 50 subscribed coins that's
// ~50 reqs/2s worst case = 25/s, well under the burst ceiling.
//
// Writes use source="rest" so the diff script can tell where the entry
// came from. WS path keeps writing source="ws" — last-write-wins.

func (a *Futures) restBackstop(ctx context.Context) {
	tick := time.NewTicker(1 * time.Second)
	defer tick.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
			a.sweepStale(ctx)
		}
	}
}

func (a *Futures) sweepStale(ctx context.Context) {
	a.symMu.RLock()
	syms := make([]string, 0, len(a.symbols))
	for s := range a.symbols {
		syms = append(syms, s)
	}
	a.symMu.RUnlock()

	if len(syms) == 0 {
		return
	}

	sem := make(chan struct{}, backstopMax)
	now := time.Now()
	for _, sym := range syms {
		entry, ok := a.store.Get("hyperliquid", sym)
		if ok && now.Sub(entry.UpdatedAt) < backstopGap {
			continue // WS is keeping it fresh; skip
		}
		sem <- struct{}{} // acquire (blocks at backstopMax in flight)
		go func(s string) {
			defer func() { <-sem }()
			a.fetchOne(ctx, s)
		}(sym)
	}
	// Drain semaphore — wait for all in-flight to finish before next tick.
	for i := 0; i < cap(sem); i++ {
		sem <- struct{}{}
	}
}

func (a *Futures) fetchOne(ctx context.Context, sym string) {
	body, err := sonic.Marshal(map[string]any{"type": "l2Book", "coin": sym})
	if err != nil {
		return
	}
	req, err := http.NewRequestWithContext(ctx, "POST", infoREST, bytes.NewReader(body))
	if err != nil {
		return
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := a.httpc.Do(req)
	if err != nil {
		log.L().Debug().Err(err).Str("coin", sym).Msg("hl rest backstop fetch failed")
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		log.L().Debug().Int("status", resp.StatusCode).Str("coin", sym).Msg("hl rest backstop non-200")
		return
	}

	var doc struct {
		Coin   string       `json:"coin"`
		Time   int64        `json:"time"`
		Levels [2][]hlLevel `json:"levels"`
	}
	if err := sonic.ConfigDefault.NewDecoder(resp.Body).Decode(&doc); err != nil {
		return
	}

	var evt time.Time
	if doc.Time > 0 {
		evt = time.UnixMilli(doc.Time)
	}
	snap := ws.Snapshot{
		Symbol:    strings.ToUpper(sym),
		Bids:      parseHLSide(doc.Levels[0]),
		Asks:      parseHLSide(doc.Levels[1]),
		EventTime: evt,
	}
	if len(snap.Bids) == 0 && len(snap.Asks) == 0 {
		return
	}
	a.store.Store("hyperliquid", snap.Symbol, snap, "rest")
}

// Hyperliquid keepalive — server sends ping frames, gorilla auto-replies.
func (a *Futures) Heartbeat() []byte                { return nil }
func (a *Futures) HeartbeatInterval() time.Duration { return 0 }
func (a *Futures) PongFor(_ []byte) []byte          { return nil }
func (a *Futures) UseLibPings() bool                { return true }

// HL drops connection with "write: broken pipe" after 4-8 subscribe frames.
func (a *Futures) SubscribeDelay() time.Duration { return 500 * time.Millisecond }
func (a *Futures) MaxSymbols() int               { return 0 }
func (a *Futures) DecompressGzip() bool          { return false }
func (a *Futures) OnReconnect()                  {}
