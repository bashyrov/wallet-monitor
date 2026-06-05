// Package aster — Aster DEX is a Binance fork. Same protocol on a different
// host, same partial-book stream format.
//
// Default channel: @depth20@100ms (depth snapshot every 100ms).
// BBO channel (ASTER_USE_BBO=1): hybrid dual-track, same as Binance/Bybit/OKX:
//   - @depth20@100ms subscribed → feeds books[token] (20-level depth state)
//   - @bookTicker     subscribed → feeds bbo[token]  (BBO overlay, event-driven)
//   mergedSnapshot splices BBO over depth top at emit time → full ladder + fast BBO.
//
// Bug-resistance: same as binance — TEXT frames, watchdog, policy backoff,
// trading-filter (Aster also returns SETTLING/BREAK status on delisted
// pairs that linger in /fapi/v1/exchangeInfo).
package aster

import (
	"context"
	"encoding/json"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

// Bare /ws — combined-stream URL + SUBSCRIBE message together caused
// Aster to drop the connection mid-subscribe ("use of closed network
// connection" right after frame 0). Bare /ws path with chunked
// SUBSCRIBE works.
const futuresWS = "wss://fstream.asterdex.com/ws"

type book struct {
	bids map[float64]float64
	asks map[float64]float64
}

type bboLevel struct {
	bidPx, bidSz float64
	askPx, askSz float64
}

type Futures struct {
	store  *cache.Store
	filter *tradingFilter
	useBBO bool // ASTER_USE_BBO=1 → dual-track (depth + BBO); false → depth only

	stateMu sync.Mutex
	books   map[string]*book
	bbo     map[string]*bboLevel
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{
		store:  store,
		filter: newTradingFilter(),
		useBBO: os.Getenv("ASTER_USE_BBO") == "1",
		books:  make(map[string]*book),
		bbo:    make(map[string]*bboLevel),
	}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("aster", snap.Symbol, snap, "ws")
	})
}

func (a *Futures) Name() string                          { return "aster" }
func (a *Futures) URL(_ context.Context) (string, error) { return futuresWS, nil }

func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
	// Filter against Aster exchangeInfo so SUBSCRIBE frames don't
	// name non-listed symbols (1008 policy violation kills the whole
	// frame on Aster the same way as Binance).
	ctx := context.Background()
	listed := make([]string, 0, len(symbols))
	for _, s := range symbols {
		if a.filter.IsTrading(ctx, strings.ToUpper(s)+"USDT") {
			listed = append(listed, s)
		}
	}
	if len(listed) == 0 {
		return nil
	}
	// Dual-track when ASTER_USE_BBO=1: send one SUBSCRIBE batch per channel.
	// Single channel per frame keeps each frame size small.
	channels := []string{"usdt@depth20@100ms"}
	if a.useBBO {
		channels = append(channels, "usdt@bookTicker")
	}
	const chunkSize = 200
	id := time.Now().UnixNano()
	frames := make([][]byte, 0, len(channels)*((len(listed)+chunkSize-1)/chunkSize))
	for ci, ch := range channels {
		for i := 0; i < len(listed); i += chunkSize {
			end := i + chunkSize
			if end > len(listed) {
				end = len(listed)
			}
			params := make([]string, end-i)
			for j, s := range listed[i:end] {
				params[j] = strings.ToLower(s) + ch
			}
			frame := map[string]any{"method": "SUBSCRIBE", "params": params, "id": id + int64(ci*1000+i)}
			b, _ := ws.MarshalJSON(frame)
			frames = append(frames, b)
		}
	}
	return frames
}

func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
	var wrap struct {
		Stream string          `json:"stream"`
		Data   json.RawMessage `json:"data"`
		Result *any            `json:"result"`
	}
	if err := ws.UnmarshalJSON(frame, &wrap); err != nil {
		return nil, err
	}
	if wrap.Result != nil {
		return nil, nil // SUBSCRIBE ack
	}

	isBookTicker := strings.HasSuffix(wrap.Stream, "@bookTicker")

	var sym string
	if wrap.Stream != "" {
		if i := strings.IndexByte(wrap.Stream, '@'); i > 0 {
			sym = strings.ToUpper(wrap.Stream[:i])
		}
	}

	dataBytes := []byte(wrap.Data)
	if len(dataBytes) == 0 {
		dataBytes = frame
	}

	if isBookTicker {
		return a.parseBookTicker(sym, dataBytes)
	}
	return a.parseDepth(sym, dataBytes)
}

// parseBookTicker handles @bookTicker frames — updates bbo state and emits
// a merged snapshot (depth + BBO overlay).
func (a *Futures) parseBookTicker(sym string, dataBytes []byte) (*ws.Snapshot, error) {
	var inner struct {
		Event   string `json:"e"` // decoy: absorb string before case-insensitive routing to EvTime
		Symbol  string `json:"s"`
		B       string `json:"b"` // best bid price
		Bq      string `json:"B"` // best bid qty
		A       string `json:"a"` // best ask price
		Aq      string `json:"A"` // best ask qty
		EvTime  int64  `json:"E"`
		TradeTs int64  `json:"T"`
	}
	if err := ws.UnmarshalJSON(dataBytes, &inner); err != nil {
		return nil, err
	}
	if sym == "" {
		sym = strings.ToUpper(inner.Symbol)
	}
	if !strings.HasSuffix(sym, "USDT") {
		return nil, nil
	}
	token := strings.TrimSuffix(sym, "USDT")
	if !a.filter.IsTrading(context.Background(), sym) {
		return nil, nil
	}

	bidPx, _ := strconv.ParseFloat(inner.B, 64)
	bidSz, _ := strconv.ParseFloat(inner.Bq, 64)
	askPx, _ := strconv.ParseFloat(inner.A, 64)
	askSz, _ := strconv.ParseFloat(inner.Aq, 64)
	if bidPx <= 0 || askPx <= 0 {
		return nil, nil
	}

	a.stateMu.Lock()
	b, ok := a.bbo[token]
	if !ok {
		b = &bboLevel{}
		a.bbo[token] = b
	}
	b.bidPx, b.bidSz = bidPx, bidSz
	b.askPx, b.askSz = askPx, askSz
	snap := a.mergedSnapshotLocked(token)
	a.stateMu.Unlock()

	switch {
	case inner.TradeTs > 0:
		snap.EventTime = time.UnixMilli(inner.TradeTs)
	case inner.EvTime > 0:
		snap.EventTime = time.UnixMilli(inner.EvTime)
	}
	return snap, nil
}

// parseDepth handles @depth20@100ms snapshot frames — full-replaces books state
// and emits a merged snapshot.
func (a *Futures) parseDepth(sym string, dataBytes []byte) (*ws.Snapshot, error) {
	var inner struct {
		Symbol  string     `json:"s"`
		EvTime  int64      `json:"E"`
		TradeTs int64      `json:"T"`
		B       [][]string `json:"b"`
		A       [][]string `json:"a"`
		Bids    [][]string `json:"bids"`
		Asks    [][]string `json:"asks"`
	}
	_ = ws.UnmarshalJSON(dataBytes, &inner)
	if sym == "" {
		sym = strings.ToUpper(inner.Symbol)
	}

	bids, asks := inner.B, inner.A
	if len(bids) == 0 && len(asks) == 0 {
		bids, asks = inner.Bids, inner.Asks
	}
	if !strings.HasSuffix(sym, "USDT") {
		return nil, nil
	}
	token := strings.TrimSuffix(sym, "USDT")

	a.stateMu.Lock()
	bk, ok := a.books[token]
	if !ok {
		bk = &book{bids: make(map[float64]float64), asks: make(map[float64]float64)}
		a.books[token] = bk
	}
	// depth20 is a full snapshot — replace wholesale.
	bk.bids = make(map[float64]float64, len(bids))
	bk.asks = make(map[float64]float64, len(asks))
	for _, r := range bids {
		if len(r) < 2 {
			continue
		}
		px, _ := strconv.ParseFloat(r[0], 64)
		sz, _ := strconv.ParseFloat(r[1], 64)
		if sz > 0 {
			bk.bids[px] = sz
		}
	}
	for _, r := range asks {
		if len(r) < 2 {
			continue
		}
		px, _ := strconv.ParseFloat(r[0], 64)
		sz, _ := strconv.ParseFloat(r[1], 64)
		if sz > 0 {
			bk.asks[px] = sz
		}
	}
	snap := a.mergedSnapshotLocked(token)
	a.stateMu.Unlock()

	switch {
	case inner.TradeTs > 0:
		snap.EventTime = time.UnixMilli(inner.TradeTs)
	case inner.EvTime > 0:
		snap.EventTime = time.UnixMilli(inner.EvTime)
	}
	return snap, nil
}

// mergedSnapshotLocked — must hold stateMu. Depth state with BBO overlay.
// Purges stale depth levels that cross the BBO before splicing (same as bitget).
func (a *Futures) mergedSnapshotLocked(token string) *ws.Snapshot {
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

func (a *Futures) Heartbeat() []byte                { return nil }
func (a *Futures) HeartbeatInterval() time.Duration { return 0 }
func (a *Futures) PongFor(_ []byte) []byte          { return nil }
func (a *Futures) UseLibPings() bool                { return true }

// Aster (Binance fork) inherits the public-WS 5 msg/s rate limit.
func (a *Futures) SubscribeDelay() time.Duration { return 250 * time.Millisecond }
func (a *Futures) MaxSymbols() int               { return 200 }
func (a *Futures) DecompressGzip() bool          { return false }
func (a *Futures) OnReconnect() {
	a.stateMu.Lock()
	a.books = make(map[string]*book)
	a.bbo = make(map[string]*bboLevel)
	a.stateMu.Unlock()
}
