// Package okx implements the OKX V5 perp orderbook WS.
//
// Channel: books — full L2 orderbook, ~400ms update cadence.
// books50-l2-tbt is tick-by-tick (10-50ms) but requires authentication
// (error code 60011). books is the public equivalent: same snapshot+delta
// wire format, same action field, just slower cadence. 400ms is plenty
// for the screener UI; the arb delta computation runs at 500ms anyway.
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
	// Separate top-of-book state fed by `bbo-tbt` (10ms public BBO).
	// Spliced into mergedSnapshot at emit time. Spot adapter doesn't
	// subscribe to bbo-tbt; left empty there.
	bbo map[string]*bboLevel
}

type book struct {
	bids map[float64]float64
	asks map[float64]float64
}

// bboLevel — single top-of-book pair from `bbo-tbt`. Zero values mean
// "no BBO update for this side yet" — splice path no-ops in that case.
type bboLevel struct {
	bidPx, bidSz float64
	askPx, askSz float64
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{
		store:      store,
		cacheKey:   "okx",
		instSuffix: "-USDT-SWAP",
		books:      make(map[string]*book),
		bbo:        make(map[string]*bboLevel),
	}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("okx", snap.Symbol, snap, "ws")
	})
}

// NewSpot — OKX V5 spot orderbook (instId form `BTC-USDT`). Same WS host
// and channel as futures; only the suffix changes. Spot doesn't subscribe
// to bbo-tbt (spot venue mostly used as the "long leg" reference in arb
// where depth cadence is fine).
func NewSpot(store *cache.Store) *ws.Runner {
	a := &Futures{
		store:      store,
		cacheKey:   "okx_spot",
		instSuffix: "-USDT",
		books:      make(map[string]*book),
		bbo:        make(map[string]*bboLevel),
	}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("okx_spot", snap.Symbol, snap, "ws")
	})
}

func (a *Futures) Name() string                          { return a.cacheKey }
func (a *Futures) URL(_ context.Context) (string, error) { return futuresWS, nil }

func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
	// OKX V5 public WS supports up to ~480 subscriptions per connection,
	// but large single frames can be silently rejected. Chunk to 100.
	//
	// Phase 2c: SWAP venue also subscribes to `bbo-tbt` (10ms public
	// top-of-book, no VIP needed) on the SAME conn. Each symbol thus
	// produces two channel subs — books for depth + bbo-tbt for top.
	// Spot adapter sticks to books only.
	const chunkSize = 100
	channels := []string{"books"}
	if a.cacheKey == "okx" {
		channels = []string{"books", "bbo-tbt"}
	}
	frames := make([][]byte, 0, (len(symbols)+chunkSize-1)/chunkSize)
	for i := 0; i < len(symbols); i += chunkSize {
		end := i + chunkSize
		if end > len(symbols) {
			end = len(symbols)
		}
		args := make([]map[string]string, 0, (end-i)*len(channels))
		for _, s := range symbols[i:end] {
			for _, ch := range channels {
				args = append(args, map[string]string{
					"channel": ch,
					"instId":  strings.ToUpper(s) + a.instSuffix,
				})
			}
		}
		b, _ := ws.MarshalJSON(map[string]any{"op": "subscribe", "args": args})
		frames = append(frames, b)
	}
	return frames
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
	isDepth := msg.Arg.Channel == "books"
	isBBO := msg.Arg.Channel == "bbo-tbt"
	if !isDepth && !isBBO {
		return nil, nil
	}
	if !strings.HasSuffix(msg.Arg.InstID, a.instSuffix) {
		return nil, nil
	}
	if len(msg.Data) == 0 {
		return nil, nil
	}
	token := strings.TrimSuffix(msg.Arg.InstID, a.instSuffix)

	if isBBO {
		return a.applyBBO(token, msg.Data[0].Bids, msg.Data[0].Asks), nil
	}

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

	return a.mergedSnapshot(token), nil
}

// applyBBO — update top-of-book state from a bbo-tbt frame and emit a
// merged Snapshot. bbo-tbt always sends both sides per frame as a full
// replace (no delta semantics); zero size means the side has no bid/ask.
func (a *Futures) applyBBO(token string, bidRows, askRows [][]string) *ws.Snapshot {
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

// mergedSnapshot — produce a Snapshot from the depth state, then splice
// BBO over the top when BBO has a strictly better price OR refreshes
// size at the existing top. Same logic as Bybit's mergedSnapshot.
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

// spliceBBOBid — see bybit/futures.go. Same semantics: BBO takes top
// when strictly better or refreshes size at the existing top.
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
	a.bbo = make(map[string]*bboLevel)
}
