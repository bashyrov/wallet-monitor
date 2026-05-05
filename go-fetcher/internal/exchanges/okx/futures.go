// Package okx implements the OKX V5 perp orderbook WS.
//
// Channel: books50-l2-tbt — tick-by-tick L2 orderbook, 50 levels.
// First push is a full snapshot (action="snapshot"), subsequent frames
// are incremental deltas (action="update"). Wire format is identical to
// the older "books" channel; only update frequency differs:
//   books         → 400ms cap (too slow for the arb page)
//   books50-l2-tbt → every book change (real-time, ~10-50ms on BTC)
// Symbol form: {BASE}-USDT-SWAP.
//
// URL: wss://ws.okx.com:8443/ws/v5/public
//
// Bug-resistance:
//   - Bug #1  : runner.SendText only
//   - Bug #2  : runner backoff
//   - Bug #20 : runner watchdog
//
// OKX V5 has its own checksum field on book diffs; we don't validate it
// (it's an integrity check, not a delivery guarantee — wrong checksum just
// means we should resync, which the watchdog also does on stale-data).
package okx

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const futuresWS = "wss://ws.okx.com:8443/ws/v5/public"

type Futures struct {
	store      *cache.Store
	cacheKey   string // "okx" | "okx_spot"
	instSuffix string // "-USDT-SWAP" | "-USDT"
	books      map[string]*book
}

type book struct {
	bids map[float64]float64
	asks map[float64]float64
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{
		store:      store,
		cacheKey:   "okx",
		instSuffix: "-USDT-SWAP",
		books:      make(map[string]*book),
	}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("okx", snap.Symbol, snap, "ws")
	})
}

// NewSpot — OKX V5 spot orderbook (instId form `BTC-USDT`). Same WS host
// and channel as futures; only the suffix changes.
func NewSpot(store *cache.Store) *ws.Runner {
	a := &Futures{
		store:      store,
		cacheKey:   "okx_spot",
		instSuffix: "-USDT",
		books:      make(map[string]*book),
	}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("okx_spot", snap.Symbol, snap, "ws")
	})
}

func (a *Futures) Name() string                          { return a.cacheKey }
func (a *Futures) URL(_ context.Context) (string, error) { return futuresWS, nil }

func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
	args := make([]map[string]string, len(symbols))
	for i, s := range symbols {
		args[i] = map[string]string{
			"channel": "books50-l2-tbt",
			"instId":  strings.ToUpper(s) + a.instSuffix,
		}
	}
	frame := map[string]any{
		"op":   "subscribe",
		"args": args,
	}
	b, _ := ws.MarshalJSON(frame)
	return [][]byte{b}
}

func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
	var msg struct {
		Event string `json:"event"`
		Arg   struct {
			Channel string `json:"channel"`
			InstID  string `json:"instId"`
		} `json:"arg"`
		Action string `json:"action"`
		Data   []struct {
			Bids [][]string `json:"bids"`
			Asks [][]string `json:"asks"`
		} `json:"data"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}

	// subscribe / unsubscribe / error events — not data
	if msg.Event != "" {
		return nil, nil
	}
	switch msg.Arg.Channel {
	case "books", "books50-l2-tbt":
		// handled below
	default:
		return nil, nil
	}
	if !strings.HasSuffix(msg.Arg.InstID, a.instSuffix) {
		return nil, nil
	}
	if len(msg.Data) == 0 {
		return nil, nil
	}
	token := strings.TrimSuffix(msg.Arg.InstID, a.instSuffix)
	bk, ok := a.books[token]
	if !ok {
		bk = &book{bids: make(map[float64]float64), asks: make(map[float64]float64)}
		a.books[token] = bk
	}
	// OKX action: snapshot | update — same merge semantics as Bybit,
	// but the bid/ask rows here are 4-element [px, sz, "0", numOrders].
	if msg.Action == "snapshot" {
		bk.bids = make(map[float64]float64, len(msg.Data[0].Bids))
		bk.asks = make(map[float64]float64, len(msg.Data[0].Asks))
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
	apply(bk.bids, msg.Data[0].Bids)
	apply(bk.asks, msg.Data[0].Asks)

	return &ws.Snapshot{
		Symbol: token,
		Bids:   ws.SortedLevels(bk.bids, ws.Bids, 200),
		Asks:   ws.SortedLevels(bk.asks, ws.Asks, 200),
	}, nil
}

// OKX uses an app-level "ping"/"pong" — server expects literal "ping"
// every 30s for the public stream. Without it the connection drops with
// 1011 after ~30s. That's identical to Bitget (bug #4 territory) but with
// a longer timeout.
func (a *Futures) Heartbeat() []byte                { return []byte("ping") }
func (a *Futures) HeartbeatInterval() time.Duration { return 25 * time.Second }
func (a *Futures) PongFor(_ []byte) []byte          { return nil }
func (a *Futures) UseLibPings() bool                { return false }
func (a *Futures) SubscribeDelay() time.Duration    { return 0 }
func (a *Futures) MaxSymbols() int                  { return 0 }
func (a *Futures) DecompressGzip() bool             { return false }

func (a *Futures) OnReconnect() {
	a.books = make(map[string]*book)
}
