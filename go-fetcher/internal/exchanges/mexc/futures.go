// Package mexc — MEXC contract (USDT-margined linear perp).
//
// URL: wss://contract.mexc.com/edge
// Subscribe: {"method":"sub.depth","param":{"symbol":"BTC_USDT","limit":20}}
//
// Inbound — incremental depth protocol:
//   First push after subscribe is a full snapshot; subsequent pushes are
//   deltas (only changed levels). Size=0 means remove the level.
//
//   {"channel":"push.depth","data":{"asks":[[px,sz,n],...],"bids":[...],"version":N},
//    "symbol":"BTC_USDT","ts":...}
//
// QUIRK — book shrinkage: `sub.depth limit:20` only pushes deltas WITHIN
// the current top-20 window. When trading eats edge levels, MEXC sends
// sz=0 to remove them but never backfills with the levels that just
// entered the top-20 from below. Over a long session the local book
// shrinks from 20 to single-digit levels. Fix: `restBackstopLoop()`
// re-fetches the full depth via REST every 30s and replaces the local
// book wholesale, capping the worst-case drift to one cycle.
//
// Heartbeat: {"method":"ping"} → server replies {"channel":"pong"}.
package mexc

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const futuresWS = "wss://contract.mexc.com/edge"
const restBase = "https://contract.mexc.com/api/v1/contract/depth"
// REST backstop intentionally disabled — orderbooks are WS-only per
// architectural decision. The one-shot REST snapshot on fresh subscribe
// (in Parse) seeds the book; from there it's pure WS push.depth deltas.
const restBackstopInterval = 0

type Futures struct {
	store *cache.Store
	mu    sync.Mutex
	books map[string]*book
	http  *http.Client
}

type book struct {
	bids   map[float64]float64
	asks   map[float64]float64
	seeded bool // first push after subscribe treated as snapshot
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{
		store: store,
		books: make(map[string]*book),
		http:  &http.Client{Timeout: 5 * time.Second},
	}
	// No periodic REST loop — WS-only steady state. The one-shot REST
	// snapshot triggered on fresh-subscribe inside Parse() seeds new
	// symbols and then deltas keep them updated.
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("mexc", snap.Symbol, snap, "ws")
	})
}

func (a *Futures) Name() string                          { return "mexc" }
func (a *Futures) URL(_ context.Context) (string, error) { return futuresWS, nil }

func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
	frames := make([][]byte, 0, len(symbols))
	for _, s := range symbols {
		// sub.depth.full pushes the FULL top-20 book on every update —
		// no deltas, no shrinkage. Same `push.depth` channel name so
		// Parse() handles it as before, but bk.seeded path now replaces
		// the book on EVERY push instead of just the first one.
		f := map[string]any{
			"method": "sub.depth.full",
			"param":  map[string]any{"symbol": strings.ToUpper(s) + "_USDT", "limit": 20},
		}
		b, _ := ws.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
	var msg struct {
		Channel string `json:"channel"`
		Symbol  string `json:"symbol"`
		Ts      int64  `json:"ts"` // ms-since-epoch event time
		Data    struct {
			Bids [][]float64 `json:"bids"`
			Asks [][]float64 `json:"asks"`
		} `json:"data"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	// sub.depth.full pushes arrive on channel `push.depth.full`; the
	// legacy `push.depth` (delta) channel we still accept in case MEXC
	// ever changes the naming.
	if msg.Channel != "push.depth.full" && msg.Channel != "push.depth" {
		return nil, nil
	}
	if !strings.HasSuffix(msg.Symbol, "_USDT") {
		return nil, nil
	}
	token := strings.TrimSuffix(msg.Symbol, "_USDT")

	a.mu.Lock()
	defer a.mu.Unlock()

	bk, ok := a.books[token]
	if !ok {
		bk = &book{bids: make(map[float64]float64), asks: make(map[float64]float64)}
		a.books[token] = bk
	}

	// With sub.depth.full every push IS the full top-20 snapshot —
	// replace the book wholesale on every message. No delta semantics,
	// no shrinkage drift.
	bk.bids = make(map[float64]float64, len(msg.Data.Bids))
	bk.asks = make(map[float64]float64, len(msg.Data.Asks))
	bk.seeded = true
	for _, r := range msg.Data.Bids {
		if len(r) >= 2 && r[1] > 0 {
			bk.bids[r[0]] = r[1]
		}
	}
	for _, r := range msg.Data.Asks {
		if len(r) >= 2 && r[1] > 0 {
			bk.asks[r[0]] = r[1]
		}
	}

	var evt time.Time
	if msg.Ts > 0 {
		evt = time.UnixMilli(msg.Ts)
	}
	return &ws.Snapshot{
		Symbol:    token,
		Bids:      ws.SortedLevels(bk.bids, ws.Bids, 200),
		Asks:      ws.SortedLevels(bk.asks, ws.Asks, 200),
		EventTime: evt,
	}, nil
}

// MEXC contract requires {"method":"ping"} every ~20s.
func (a *Futures) Heartbeat() []byte                { return []byte(`{"method":"ping"}`) }
func (a *Futures) HeartbeatInterval() time.Duration { return 18 * time.Second }
func (a *Futures) PongFor(_ []byte) []byte          { return nil }
func (a *Futures) UseLibPings() bool                { return false }
func (a *Futures) SubscribeDelay() time.Duration    { return 0 }
func (a *Futures) MaxSymbols() int                  { return 0 }
func (a *Futures) DecompressGzip() bool             { return false }

func (a *Futures) OnReconnect() {
	// Clear all books — next push per symbol will be treated as a fresh snapshot.
	a.mu.Lock()
	a.books = make(map[string]*book)
	a.mu.Unlock()
}

// restBackstopLoop periodically pulls the full depth via REST for every
// symbol we currently track and replaces the local book. Counters MEXC's
// top-20 delta protocol drifting down to single-digit levels as edge
// rows are filled without backfill.
func (a *Futures) restBackstopLoop() {
	t := time.NewTicker(restBackstopInterval)
	defer t.Stop()
	for range t.C {
		a.mu.Lock()
		syms := make([]string, 0, len(a.books))
		for s := range a.books {
			syms = append(syms, s)
		}
		a.mu.Unlock()
		for _, sym := range syms {
			a.fetchAndReplace(sym)
		}
	}
}

func (a *Futures) fetchAndReplace(sym string) {
	url := restBase + "/" + strings.ToUpper(sym) + "_USDT"
	resp, err := a.http.Get(url)
	if err != nil {
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return
	}
	var payload struct {
		Success bool `json:"success"`
		Data    struct {
			Bids [][]float64 `json:"bids"`
			Asks [][]float64 `json:"asks"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return
	}
	if !payload.Success || (len(payload.Data.Bids) == 0 && len(payload.Data.Asks) == 0) {
		return
	}
	a.mu.Lock()
	bk, ok := a.books[sym]
	if !ok {
		// Symbol got unsubscribed between snapshot start and now — drop.
		a.mu.Unlock()
		return
	}
	bk.bids = make(map[float64]float64, len(payload.Data.Bids))
	bk.asks = make(map[float64]float64, len(payload.Data.Asks))
	for _, r := range payload.Data.Bids {
		if len(r) >= 2 && r[1] > 0 {
			bk.bids[r[0]] = r[1]
		}
	}
	for _, r := range payload.Data.Asks {
		if len(r) >= 2 && r[1] > 0 {
			bk.asks[r[0]] = r[1]
		}
	}
	bk.seeded = true
	bids := ws.SortedLevels(bk.bids, ws.Bids, 200)
	asks := ws.SortedLevels(bk.asks, ws.Asks, 200)
	a.mu.Unlock()
	a.store.Store("mexc", sym, ws.Snapshot{Symbol: sym, Bids: bids, Asks: asks}, "rest")
}
