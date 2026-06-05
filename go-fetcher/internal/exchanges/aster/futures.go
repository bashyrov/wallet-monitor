// Package aster — Aster DEX is a Binance fork. Same protocol on a different
// host, same partial-book stream format. We embed *binance.Futures
// behaviourally by copying the parser and swapping the URL — embedding
// directly would force one shared exchangeInfo cache between the two
// venues, which we don't want (different symbol sets).
//
// Default channel: @depth20@100ms (depth snapshot every 100ms).
// BBO channel (ASTER_USE_BBO=1): @bookTicker — real-time top-of-book,
// event-driven (same protocol as Binance @bookTicker).
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
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

// Bare /ws — combined-stream URL + SUBSCRIBE message together caused
// Aster to drop the connection mid-subscribe ("use of closed network
// connection" right after frame 0). Bare /ws path with chunked
// SUBSCRIBE works.
const futuresWS = "wss://fstream.asterdex.com/ws"

type Futures struct {
	store  *cache.Store
	filter *tradingFilter
	useBBO bool // ASTER_USE_BBO=1 → @bookTicker; false → @depth20@100ms
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{
		store:  store,
		filter: newTradingFilter(),
		useBBO: os.Getenv("ASTER_USE_BBO") == "1",
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
	suffix := "usdt@depth20@100ms"
	if a.useBBO {
		suffix = "usdt@bookTicker"
	}
	const chunkSize = 200
	frames := make([][]byte, 0, (len(listed)+chunkSize-1)/chunkSize)
	id := time.Now().UnixNano()
	for i := 0; i < len(listed); i += chunkSize {
		end := i + chunkSize
		if end > len(listed) {
			end = len(listed)
		}
		params := make([]string, end-i)
		for j, s := range listed[i:end] {
			params[j] = strings.ToLower(s) + suffix
		}
		frame := map[string]any{"method": "SUBSCRIBE", "params": params, "id": id + int64(i)}
		b, _ := ws.MarshalJSON(frame)
		frames = append(frames, b)
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

// parseBookTicker handles @bookTicker frames.
// Wire (via bare /ws): {"stream":"btcusdt@bookTicker","data":{
//
//	"e":"bookTicker","u":N,"s":"BTCUSDT","b":"px","B":"qty","a":"px","A":"qty","T":N,"E":N}}
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

	snap := &ws.Snapshot{
		Symbol: token,
		Bids:   []ws.Level{{bidPx, bidSz}},
		Asks:   []ws.Level{{askPx, askSz}},
	}
	switch {
	case inner.TradeTs > 0:
		snap.EventTime = time.UnixMilli(inner.TradeTs)
	case inner.EvTime > 0:
		snap.EventTime = time.UnixMilli(inner.EvTime)
	}
	return snap, nil
}

// parseDepth handles @depth20@100ms snapshot frames.
func (a *Futures) parseDepth(sym string, dataBytes []byte) (*ws.Snapshot, error) {
	var inner struct {
		Symbol  string     `json:"s"`
		EvTime  int64      `json:"E"` // event time ms
		TradeTs int64      `json:"T"` // transaction time ms
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

	snap := &ws.Snapshot{Symbol: token}
	for _, r := range bids {
		if len(r) < 2 {
			continue
		}
		px, _ := strconv.ParseFloat(r[0], 64)
		sz, _ := strconv.ParseFloat(r[1], 64)
		if sz > 0 {
			snap.Bids = append(snap.Bids, ws.Level{px, sz})
		}
	}
	for _, r := range asks {
		if len(r) < 2 {
			continue
		}
		px, _ := strconv.ParseFloat(r[0], 64)
		sz, _ := strconv.ParseFloat(r[1], 64)
		if sz > 0 {
			snap.Asks = append(snap.Asks, ws.Level{px, sz})
		}
	}
	switch {
	case inner.TradeTs > 0:
		snap.EventTime = time.UnixMilli(inner.TradeTs)
	case inner.EvTime > 0:
		snap.EventTime = time.UnixMilli(inner.EvTime)
	}
	return snap, nil
}

func (a *Futures) Heartbeat() []byte                { return nil }
func (a *Futures) HeartbeatInterval() time.Duration { return 0 }
func (a *Futures) PongFor(_ []byte) []byte          { return nil }
func (a *Futures) UseLibPings() bool                { return true }

// Aster (Binance fork) inherits the public-WS 5 msg/s rate limit. With
// chunked SUBSCRIBE frames, a 250ms delay keeps us under that ceiling.
func (a *Futures) SubscribeDelay() time.Duration { return 250 * time.Millisecond }
func (a *Futures) MaxSymbols() int               { return 200 }
func (a *Futures) DecompressGzip() bool          { return false }
func (a *Futures) OnReconnect()                  {}
