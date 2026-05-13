// Package gate — Gate.io USDT-margined perp orderbook (Phase 2d migrate).
//
// URL: wss://fx-ws.gateio.ws/v4/ws/usdt
//
// Channel: `futures.order_book_update` (incremental diff, Binance-style
// U/u semantics) replacing the older `futures.order_book` full-push.
//
// Subscribe payload: [contract, interval, level]
//   {"time":N, "channel":"futures.order_book_update", "event":"subscribe",
//    "payload":["BTC_USDT","100ms","20"]}
//
// Inbound delta:
//   {"channel":"futures.order_book_update", "event":"update",
//    "result":{"t":1605630238094,"s":"BTC_USDT","U":N,"u":N+5,
//              "b":[{"p":"...","s":0}],"a":[{"p":"...","s":N}]}}
//
// Bootstrap (per symbol):
//   1. WS subscribes; deltas start flowing — buffered until snapshot lands.
//   2. REST GET /api/v4/futures/usdt/order_book?contract=X&limit=20&with_id=true
//      → {"id":N, "bids":[...], "asks":[...]}
//   3. Snapshot id = baseID. Apply only buffered events where u > baseID,
//      AND the first applied event must satisfy U <= baseID+1 <= u.
//   4. Subsequent events: must have U == prevU+1 (contiguous), else gap
//      → drop state + re-bootstrap.
package gate

import (
	"context"
	"fmt"
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

const futuresWS = "wss://fx-ws.gateio.ws/v4/ws/usdt"

// restSnapshot is overridable in tests.
var restSnapshot = "https://api.gateio.ws/api/v4/futures/usdt/order_book?contract=%s&limit=20&with_id=true"

type Futures struct {
	store *cache.Store
	mu    sync.Mutex
	books map[string]*book
}

type bookLevel struct {
	P string  `json:"p"`
	S float64 `json:"s"`
}

type book struct {
	bids   map[float64]float64
	asks   map[float64]float64
	baseID uint64
	lastU  uint64
	seeded bool
	buffer []bufferedEvent
}

type bufferedEvent struct {
	U     uint64
	U2    uint64 // u (last update id)
	bids  []bookLevel
	asks  []bookLevel
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{store: store, books: make(map[string]*book)}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("gate", snap.Symbol, snap, "ws")
	})
}

func (a *Futures) Name() string                          { return "gate" }
func (a *Futures) URL(_ context.Context) (string, error) { return futuresWS, nil }

func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
	frames := make([][]byte, 0, len(symbols))
	for _, s := range symbols {
		go a.seedREST(s)
		f := map[string]any{
			"time":    time.Now().Unix(),
			"channel": "futures.order_book_update",
			"event":   "subscribe",
			"payload": []string{strings.ToUpper(s) + "_USDT", "100ms", "20"},
		}
		b, _ := ws.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

// seedREST fetches the REST snapshot (async — does not block subscribe)
// and merges it into book state. WS deltas arriving before the snapshot
// land in book.buffer; we drain them after seeding.
func (a *Futures) seedREST(symbol string) {
	url := fmt.Sprintf(restSnapshot, strings.ToUpper(symbol)+"_USDT")
	cl := &http.Client{Timeout: 6 * time.Second}
	resp, err := cl.Get(url)
	if err != nil {
		return
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return
	}
	var doc struct {
		ID   uint64      `json:"id"`
		Bids []bookLevel `json:"bids"`
		Asks []bookLevel `json:"asks"`
	}
	if err := sonic.Unmarshal(body, &doc); err != nil {
		return
	}
	if doc.ID == 0 {
		return
	}
	token := strings.ToUpper(symbol)

	a.mu.Lock()
	defer a.mu.Unlock()
	bk := a.bookFor(token)
	bk.bids = make(map[float64]float64, len(doc.Bids))
	bk.asks = make(map[float64]float64, len(doc.Asks))
	for _, lvl := range doc.Bids {
		px, _ := strconv.ParseFloat(lvl.P, 64)
		if lvl.S > 0 {
			bk.bids[px] = lvl.S
		}
	}
	for _, lvl := range doc.Asks {
		px, _ := strconv.ParseFloat(lvl.P, 64)
		if lvl.S > 0 {
			bk.asks[px] = lvl.S
		}
	}
	bk.baseID = doc.ID
	a.drainBuffer(bk)
}

// drainBuffer applies any events queued before the REST snapshot landed.
// Per Gate docs the first valid event must satisfy U <= baseID+1 <= u.
// Buffer entries older than baseID are discarded.
func (a *Futures) drainBuffer(bk *book) {
	if !bk.seeded {
		for _, ev := range bk.buffer {
			if ev.U2 <= bk.baseID {
				continue
			}
			if !(ev.U <= bk.baseID+1 && bk.baseID+1 <= ev.U2) {
				// gap at bootstrap — discard buffer, force re-seed
				bk.buffer = nil
				bk.baseID = 0
				return
			}
			a.applyDelta(bk, ev.bids, ev.asks)
			bk.lastU = ev.U2
			bk.seeded = true
		}
	}
	// After first seeded event, the remaining buffer entries should be
	// contiguous on lastU. Drop any that violate.
	if bk.seeded {
		for _, ev := range bk.buffer {
			if ev.U2 <= bk.lastU {
				continue
			}
			if ev.U != bk.lastU+1 {
				bk.bids = make(map[float64]float64)
				bk.asks = make(map[float64]float64)
				bk.baseID = 0
				bk.seeded = false
				bk.buffer = nil
				return
			}
			a.applyDelta(bk, ev.bids, ev.asks)
			bk.lastU = ev.U2
		}
	}
	bk.buffer = nil
}

func (a *Futures) bookFor(token string) *book {
	bk, ok := a.books[token]
	if !ok {
		bk = &book{
			bids: make(map[float64]float64),
			asks: make(map[float64]float64),
		}
		a.books[token] = bk
	}
	return bk
}

func (a *Futures) applyDelta(bk *book, bids, asks []bookLevel) {
	for _, lvl := range bids {
		px, _ := strconv.ParseFloat(lvl.P, 64)
		if lvl.S == 0 {
			delete(bk.bids, px)
		} else {
			bk.bids[px] = lvl.S
		}
	}
	for _, lvl := range asks {
		px, _ := strconv.ParseFloat(lvl.P, 64)
		if lvl.S == 0 {
			delete(bk.asks, px)
		} else {
			bk.asks[px] = lvl.S
		}
	}
}

func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
	var msg struct {
		Channel string `json:"channel"`
		Event   string `json:"event"`
		Result  struct {
			Symbol string      `json:"s"`
			U      uint64      `json:"U"`
			U2     uint64      `json:"u"`
			Bids   []bookLevel `json:"b"`
			Asks   []bookLevel `json:"a"`
		} `json:"result"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Channel != "futures.order_book_update" {
		return nil, nil
	}
	if msg.Event != "update" {
		// subscribe ack carries event="subscribe" — ignore
		return nil, nil
	}
	contract := msg.Result.Symbol
	if !strings.HasSuffix(contract, "_USDT") {
		return nil, nil
	}
	token := strings.TrimSuffix(contract, "_USDT")

	a.mu.Lock()
	defer a.mu.Unlock()
	bk := a.bookFor(token)

	// Not seeded yet (REST snapshot still in flight): buffer the event.
	if bk.baseID == 0 {
		bk.buffer = append(bk.buffer, bufferedEvent{
			U:    msg.Result.U,
			U2:   msg.Result.U2,
			bids: msg.Result.Bids,
			asks: msg.Result.Asks,
		})
		return nil, nil
	}

	// Snapshot has landed. If we haven't applied any delta yet, this
	// event must straddle baseID+1.
	if !bk.seeded {
		if msg.Result.U2 <= bk.baseID {
			// stale — already covered by snapshot
			return nil, nil
		}
		if !(msg.Result.U <= bk.baseID+1 && bk.baseID+1 <= msg.Result.U2) {
			// gap at bootstrap edge — reset, await new snapshot
			bk.baseID = 0
			return nil, nil
		}
		a.applyDelta(bk, msg.Result.Bids, msg.Result.Asks)
		bk.lastU = msg.Result.U2
		bk.seeded = true
	} else {
		// Steady state: must be contiguous.
		if msg.Result.U != bk.lastU+1 {
			// gap — reset state, force re-bootstrap
			bk.bids = make(map[float64]float64)
			bk.asks = make(map[float64]float64)
			bk.baseID = 0
			bk.seeded = false
			bk.lastU = 0
			go a.seedREST(token)
			return nil, nil
		}
		a.applyDelta(bk, msg.Result.Bids, msg.Result.Asks)
		bk.lastU = msg.Result.U2
	}

	return &ws.Snapshot{
		Symbol: token,
		Bids:   ws.SortedLevels(bk.bids, ws.Bids, 200),
		Asks:   ws.SortedLevels(bk.asks, ws.Asks, 200),
	}, nil
}

// Gate uses lib-level WS pings for keepalive. No app-level frame.
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
