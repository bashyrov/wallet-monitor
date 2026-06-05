// Package gate — Gate.io USDT-margined perp orderbook (Phase 2d migrate).
//
// URL: wss://fx-ws.gateio.ws/v4/ws/usdt
//
// Default channel: `futures.order_book_update` (incremental diff).
// BBO channel (GATE_USE_BBO=1): `futures.book_ticker` — real-time best
// bid/ask, event-driven, ~10ms cadence (significantly faster than the
// 100ms incremental channel).
//
// Subscribe (order_book_update):
//
//	{"time":N, "channel":"futures.order_book_update", "event":"subscribe",
//	 "payload":["BTC_USDT","100ms","20"]}
//
// Subscribe (book_ticker):
//
//	{"time":N, "channel":"futures.book_ticker", "event":"subscribe",
//	 "payload":["BTC_USDT"]}
//
// Inbound (book_ticker):
//
//	{"channel":"futures.book_ticker","event":"update",
//	 "result":{"t":N,"u":N,"s":"BTC_USDT","b":"px","B":"sz","a":"px","A":"sz"}}
package gate

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
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
	store  *cache.Store
	mu     sync.Mutex
	books  map[string]*book
	useBBO bool // GATE_USE_BBO=1 → futures.book_ticker; false → order_book_update
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
	a := &Futures{
		store:  store,
		books:  make(map[string]*book),
		useBBO: os.Getenv("GATE_USE_BBO") == "1",
	}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("gate", snap.Symbol, snap, "ws")
	})
}

func (a *Futures) Name() string                          { return "gate" }
func (a *Futures) URL(_ context.Context) (string, error) { return futuresWS, nil }

func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
	if a.useBBO {
		return a.buildBBOSubscribe(symbols)
	}
	return a.buildDepthSubscribe(symbols)
}

// buildBBOSubscribe batches up to 50 symbols per futures.book_ticker frame.
// Gate WS accepts an array of contracts in one payload, so 300 symbols →
// 6 frames instead of 300 one-per-symbol frames that overload the server.
func (a *Futures) buildBBOSubscribe(symbols []string) [][]byte {
	const chunkSize = 50
	frames := make([][]byte, 0, (len(symbols)+chunkSize-1)/chunkSize)
	for i := 0; i < len(symbols); i += chunkSize {
		end := i + chunkSize
		if end > len(symbols) {
			end = len(symbols)
		}
		contracts := make([]string, end-i)
		for j, s := range symbols[i:end] {
			contracts[j] = strings.ToUpper(s) + "_USDT"
		}
		f := map[string]any{
			"time":    time.Now().Unix(),
			"channel": "futures.book_ticker",
			"event":   "subscribe",
			"payload": contracts,
		}
		b, _ := ws.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

func (a *Futures) buildDepthSubscribe(symbols []string) [][]byte {
	frames := make([][]byte, 0, len(symbols))
	for _, s := range symbols {
		contract := strings.ToUpper(s) + "_USDT"
		go a.seedREST(s)
		f := map[string]any{
			"time":    time.Now().Unix(),
			"channel": "futures.order_book_update",
			"event":   "subscribe",
			"payload": []string{contract, "100ms", "20"},
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

// Parse routes incoming frames to the appropriate handler based on channel.
func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
	var envelope struct {
		Channel string          `json:"channel"`
		Event   string          `json:"event"`
		Result  json.RawMessage `json:"result"`
	}
	if err := ws.UnmarshalJSON(frame, &envelope); err != nil {
		return nil, err
	}
	switch envelope.Channel {
	case "futures.book_ticker":
		if envelope.Event != "update" {
			return nil, nil
		}
		return a.parseBookTicker(envelope.Result)
	case "futures.order_book_update":
		if envelope.Event != "update" {
			return nil, nil
		}
		return a.parseDepthUpdate(envelope.Result)
	default:
		return nil, nil
	}
}

// parseBookTicker handles the futures.book_ticker BBO frame.
// Gate sends prices/quantities as NUMBERS (float64), not strings.
// Wire: {"t":N,"u":N,"s":"BTC_USDT","b":bidPx,"B":bidSz,"a":askPx,"A":askSz}
func (a *Futures) parseBookTicker(raw json.RawMessage) (*ws.Snapshot, error) {
	var r struct {
		T  int64   `json:"t"` // ms timestamp
		S  string  `json:"s"` // "BTC_USDT"
		B  float64 `json:"b"` // best bid price
		Bq float64 `json:"B"` // best bid qty
		A  float64 `json:"a"` // best ask price
		Aq float64 `json:"A"` // best ask qty
	}
	if err := ws.UnmarshalJSON(raw, &r); err != nil {
		return nil, err
	}
	if !strings.HasSuffix(r.S, "_USDT") {
		return nil, nil
	}
	token := strings.TrimSuffix(r.S, "_USDT")
	if r.B <= 0 || r.A <= 0 {
		return nil, nil
	}
	var evt time.Time
	if r.T > 0 {
		evt = time.UnixMilli(r.T)
	}
	return &ws.Snapshot{
		Symbol:    token,
		Bids:      []ws.Level{{r.B, r.Bq}},
		Asks:      []ws.Level{{r.A, r.Aq}},
		EventTime: evt,
	}, nil
}

// parseDepthUpdate handles the futures.order_book_update incremental frame.
func (a *Futures) parseDepthUpdate(raw json.RawMessage) (*ws.Snapshot, error) {
	var result struct {
		Symbol string      `json:"s"`
		T      int64       `json:"t"` // ms-since-epoch event time
		U      uint64      `json:"U"`
		U2     uint64      `json:"u"`
		Bids   []bookLevel `json:"b"`
		Asks   []bookLevel `json:"a"`
	}
	if err := ws.UnmarshalJSON(raw, &result); err != nil {
		return nil, err
	}
	contract := result.Symbol
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
			U:    result.U,
			U2:   result.U2,
			bids: result.Bids,
			asks: result.Asks,
		})
		return nil, nil
	}

	// Snapshot has landed. If we haven't applied any delta yet, this
	// event must straddle baseID+1.
	if !bk.seeded {
		if result.U2 <= bk.baseID {
			return nil, nil
		}
		if !(result.U <= bk.baseID+1 && bk.baseID+1 <= result.U2) {
			bk.baseID = 0
			return nil, nil
		}
		a.applyDelta(bk, result.Bids, result.Asks)
		bk.lastU = result.U2
		bk.seeded = true
	} else {
		if result.U != bk.lastU+1 {
			bk.bids = make(map[float64]float64)
			bk.asks = make(map[float64]float64)
			bk.baseID = 0
			bk.seeded = false
			bk.lastU = 0
			go a.seedREST(token)
			return nil, nil
		}
		a.applyDelta(bk, result.Bids, result.Asks)
		bk.lastU = result.U2
	}

	var evt time.Time
	if result.T > 0 {
		evt = time.UnixMilli(result.T)
	}
	return &ws.Snapshot{
		Symbol:    token,
		Bids:      ws.SortedLevels(bk.bids, ws.Bids, 200),
		Asks:      ws.SortedLevels(bk.asks, ws.Asks, 200),
		EventTime: evt,
	}, nil
}

// Gate uses lib-level WS pings for keepalive. No app-level frame.
func (a *Futures) Heartbeat() []byte                { return nil }
func (a *Futures) HeartbeatInterval() time.Duration { return 0 }
func (a *Futures) PongFor(_ []byte) []byte          { return nil }
func (a *Futures) UseLibPings() bool                { return true }
// SubscribeDelay: BBO mode sends batched frames (50 contracts/frame);
// add 200ms between frames to avoid triggering gate's rate limiter.
// Depth mode sends one frame per symbol — no delay needed (gate handles it).
func (a *Futures) SubscribeDelay() time.Duration {
	if a.useBBO {
		return 200 * time.Millisecond
	}
	return 0
}
func (a *Futures) MaxSymbols() int { return 0 }
func (a *Futures) DecompressGzip() bool             { return false }

func (a *Futures) OnReconnect() {
	a.mu.Lock()
	a.books = make(map[string]*book)
	a.mu.Unlock()
}
