// Package backpack — Backpack Exchange perp orderbook.
//
// URL: wss://ws.backpack.exchange
// Subscribe: {"method":"SUBSCRIBE","params":["depth.<BASE>_USDC_PERP"]}
//
// Inbound (DELTA stream — single-level deltas per push):
//   {"stream":"depth.BTC_USDC_PERP",
//    "data":{"e":"depth","E":...,"s":"BTC_USDC_PERP",
//            "b":[["px","sz"]] | [],   // single bid delta or empty
//            "a":[["px","sz"]] | [],   // single ask delta or empty
//            "u":<lastUpdateId>}}
//
// QUIRKS:
//   - Symbol form: <BASE>_USDC_PERP (USDC quote, not USDT)
//   - Stream is DIFF-only — full book must be REST-seeded then merged.
//   - Probe confirmed pushes single-level deltas at ~50-100ms.
package backpack

import (
	"context"
	"io"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bytedance/sonic"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const futuresWS = "wss://ws.backpack.exchange"
// Backpack's REST depth endpoint rejects `limit` as enum-mismatch — drop
// the query param and accept the default depth (returns full book).
const restDepth = "https://api.backpack.exchange/api/v1/depth?symbol=%s_USDC_PERP"

type Futures struct {
	store *cache.Store
	mu    sync.Mutex
	books map[string]*book
}

type book struct {
	bids   map[float64]float64
	asks   map[float64]float64
	seeded bool
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{store: store, books: make(map[string]*book)}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("backpack", snap.Symbol, snap, "ws")
	})
}

func (a *Futures) Name() string                          { return "backpack" }
func (a *Futures) URL(_ context.Context) (string, error) { return futuresWS, nil }

func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
	// Seed REST snapshots in parallel — async so we don't block the
	// subscribe frame. Deltas applied on top will be ahead of seed
	// briefly; that's fine, the next delta supersedes anyway.
	for _, s := range symbols {
		go a.seedRest(s)
	}
	params := make([]string, len(symbols))
	for i, s := range symbols {
		params[i] = "depth." + strings.ToUpper(s) + "_USDC_PERP"
	}
	frame := map[string]any{"method": "SUBSCRIBE", "params": params}
	b, _ := ws.MarshalJSON(frame)
	return [][]byte{b}
}

func (a *Futures) seedRest(symbol string) {
	url := strings.ReplaceAll(restDepth, "%s", strings.ToUpper(symbol))
	cl := &http.Client{Timeout: 6 * time.Second}
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return
	}
	req.Header.Set("User-Agent", "Mozilla/5.0 avalant-fetcher/go")
	resp, err := cl.Do(req)
	if err != nil {
		return
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return
	}
	var doc struct {
		Bids [][]string `json:"bids"`
		Asks [][]string `json:"asks"`
	}
	if err := sonic.Unmarshal(body, &doc); err != nil {
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	bk, ok := a.books[symbol]
	if !ok {
		bk = &book{bids: make(map[float64]float64), asks: make(map[float64]float64)}
		a.books[symbol] = bk
	}
	for _, r := range doc.Bids {
		if len(r) < 2 {
			continue
		}
		px, _ := strconv.ParseFloat(r[0], 64)
		sz, _ := strconv.ParseFloat(r[1], 64)
		if sz > 0 {
			bk.bids[px] = sz
		}
	}
	for _, r := range doc.Asks {
		if len(r) < 2 {
			continue
		}
		px, _ := strconv.ParseFloat(r[0], 64)
		sz, _ := strconv.ParseFloat(r[1], 64)
		if sz > 0 {
			bk.asks[px] = sz
		}
	}
	bk.seeded = true
}

func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
	var msg struct {
		Stream string `json:"stream"`
		Data   struct {
			Symbol string     `json:"s"`
			Bids   [][]string `json:"b"`
			Asks   [][]string `json:"a"`
			E      int64      `json:"E"` // event time μs (Backpack uses microseconds)
		} `json:"data"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if !strings.HasPrefix(msg.Stream, "depth.") {
		return nil, nil
	}
	sym := msg.Data.Symbol
	if !strings.HasSuffix(sym, "_USDC_PERP") {
		return nil, nil
	}
	token := strings.TrimSuffix(sym, "_USDC_PERP")

	a.mu.Lock()
	bk, ok := a.books[token]
	if !ok {
		bk = &book{bids: make(map[float64]float64), asks: make(map[float64]float64)}
		a.books[token] = bk
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
	apply(bk.bids, msg.Data.Bids)
	apply(bk.asks, msg.Data.Asks)
	bids := ws.SortedLevels(bk.bids, ws.Bids, 200)
	asks := ws.SortedLevels(bk.asks, ws.Asks, 200)
	a.mu.Unlock()

	var evt time.Time
	if msg.Data.E > 0 {
		// Backpack uses microseconds, not milliseconds.
		evt = time.UnixMicro(msg.Data.E)
	}
	return &ws.Snapshot{Symbol: token, Bids: bids, Asks: asks, EventTime: evt}, nil
}

func (a *Futures) Heartbeat() []byte                { return nil }
func (a *Futures) HeartbeatInterval() time.Duration { return 0 }
func (a *Futures) PongFor(_ []byte) []byte          { return nil }
func (a *Futures) UseLibPings() bool                { return true }
func (a *Futures) SubscribeDelay() time.Duration    { return 0 }
func (a *Futures) MaxSymbols() int                  { return 0 }
func (a *Futures) DecompressGzip() bool             { return false }

func (a *Futures) OnReconnect() {
	a.mu.Lock()
	a.books = make(map[string]*book)
	a.mu.Unlock()
}
