// Binance spot orderbook WS.
//
// URL: wss://stream.binance.com:9443/stream — combined endpoint, streams
// attached via SUBSCRIBE frame.
//
// Stream layout (mirrors futures Phase 2a):
//   <sym>usdt@depth20@100ms  — partial-book snapshot (top-20) with no
//                              event time field
//   <sym>usdt@bookTicker     — top-of-book only, but carries `E` event
//                              time so the latency histogram can score
//                              spot end-to-end
//
// Adapter maintains separate `depthState` + `bboState` per symbol; the
// merged snapshot splices BBO over the depth top-of-book at emit time.
// EventTime is taken from bookTicker frames (depth has no usable ts).
package binance

import (
	"context"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const spotCombinedBase = "wss://stream.binance.com:9443/stream"

type Spot struct {
	store *cache.Store

	mu        sync.Mutex
	depthState map[string][2][]ws.Level // sym → [bids, asks]
	bboState  map[string]spotBBO
}

type spotBBO struct {
	bidPx, bidSz float64
	askPx, askSz float64
	eventTime    time.Time
}

func NewSpot(store *cache.Store) *ws.Runner {
	a := &Spot{
		store:      store,
		depthState: make(map[string][2][]ws.Level),
		bboState:   make(map[string]spotBBO),
	}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("binance_spot", snap.Symbol, snap, "ws")
	})
}

func (a *Spot) Name() string                          { return "binance_spot" }
func (a *Spot) URL(_ context.Context) (string, error) { return spotCombinedBase, nil }

// BuildSubscribe emits depth20@100ms + bookTicker streams per symbol.
// HOTFIX 2026-05-13: drop @bookTicker subscription. Doubling streams
// per symbol (200 syms × 2 = 400) triggered intermittent 1008 policy
// closes on Binance spot too. The bookTicker on spot doesn't carry the
// `E` event-time field anyway (see Parse comment), so dropping it
// loses only the BBO splice — which depth20 already provides as
// top-of-book.
func (a *Spot) BuildSubscribe(symbols []string) [][]byte {
	if len(symbols) == 0 {
		return nil
	}
	const chunkSize = 200
	frames := make([][]byte, 0, (len(symbols)+chunkSize-1)/chunkSize)
	id := time.Now().UnixNano()
	for i := 0; i < len(symbols); i += chunkSize {
		end := i + chunkSize
		if end > len(symbols) {
			end = len(symbols)
		}
		params := make([]string, end-i)
		for j, s := range symbols[i:end] {
			params[j] = strings.ToLower(s) + "usdt@depth20@100ms"
		}
		frame := map[string]any{
			"method": "SUBSCRIBE",
			"params": params,
			"id":     id + int64(i),
		}
		b, _ := ws.MarshalJSON(frame)
		frames = append(frames, b)
	}
	return frames
}

// Parse routes by stream suffix: @depth20 → depth state, @bookTicker →
// BBO state. Emits a merged snapshot in either case.
func (a *Spot) Parse(frame []byte) (*ws.Snapshot, error) {
	var wrap struct {
		Stream string `json:"stream"`
		Data   struct {
			// depth20 fields
			Bids [][]string `json:"bids"`
			Asks [][]string `json:"asks"`
			// bookTicker fields
			B  string `json:"b"`
			BS string `json:"B"`
			A  string `json:"a"`
			AS string `json:"A"`
			// bookTicker carries E only on UFutures-format frames; spot
			// `@bookTicker` does NOT carry E — only `u` (update id).
			// Fall back to envelope-receive time when absent.
			E int64 `json:"E"`
		} `json:"data"`
		Result *any `json:"result"`
	}
	if err := ws.UnmarshalJSON(frame, &wrap); err != nil {
		return nil, err
	}
	if wrap.Result != nil {
		return nil, nil
	}
	if wrap.Stream == "" {
		return nil, nil
	}
	parts := strings.SplitN(wrap.Stream, "@", 2)
	if len(parts) < 2 {
		return nil, nil
	}
	sym := strings.ToUpper(parts[0])
	if !strings.HasSuffix(sym, "USDT") {
		return nil, nil
	}
	token := strings.TrimSuffix(sym, "USDT")
	suffix := parts[1]

	a.mu.Lock()
	defer a.mu.Unlock()

	switch {
	case strings.HasPrefix(suffix, "depth20"):
		bids := parseSpotLevels(wrap.Data.Bids)
		asks := parseSpotLevels(wrap.Data.Asks)
		a.depthState[token] = [2][]ws.Level{bids, asks}
	case suffix == "bookTicker":
		bidPx, _ := strconv.ParseFloat(wrap.Data.B, 64)
		bidSz, _ := strconv.ParseFloat(wrap.Data.BS, 64)
		askPx, _ := strconv.ParseFloat(wrap.Data.A, 64)
		askSz, _ := strconv.ParseFloat(wrap.Data.AS, 64)
		evt := time.Time{}
		if wrap.Data.E > 0 {
			evt = time.UnixMilli(wrap.Data.E)
		}
		a.bboState[token] = spotBBO{bidPx: bidPx, bidSz: bidSz, askPx: askPx, askSz: askSz, eventTime: evt}
	default:
		return nil, nil
	}

	depth := a.depthState[token]
	bbo := a.bboState[token]
	bids := spliceSpotBid(depth[0], bbo.bidPx, bbo.bidSz)
	asks := spliceSpotAsk(depth[1], bbo.askPx, bbo.askSz)
	return &ws.Snapshot{
		Symbol:    token,
		Bids:      bids,
		Asks:      asks,
		EventTime: bbo.eventTime,
	}, nil
}

func parseSpotLevels(rows [][]string) []ws.Level {
	out := make([]ws.Level, 0, len(rows))
	for _, r := range rows {
		if len(r) < 2 {
			continue
		}
		px, perr := strconv.ParseFloat(r[0], 64)
		sz, serr := strconv.ParseFloat(r[1], 64)
		if perr != nil || serr != nil || sz <= 0 {
			continue
		}
		out = append(out, ws.Level{px, sz})
	}
	return out
}

func spliceSpotBid(bids []ws.Level, bboPx, bboSz float64) []ws.Level {
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
		out := append([]ws.Level(nil), bids...)
		out[0][1] = bboSz
		return out
	}
	return bids
}

func spliceSpotAsk(asks []ws.Level, bboPx, bboSz float64) []ws.Level {
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
		out := append([]ws.Level(nil), asks...)
		out[0][1] = bboSz
		return out
	}
	return asks
}

func (a *Spot) Heartbeat() []byte                { return nil }
func (a *Spot) HeartbeatInterval() time.Duration { return 0 }
func (a *Spot) PongFor(_ []byte) []byte          { return nil }
func (a *Spot) UseLibPings() bool                { return true }
func (a *Spot) SubscribeDelay() time.Duration    { return 0 }
func (a *Spot) MaxSymbols() int                  { return 200 }
func (a *Spot) DecompressGzip() bool             { return false }

func (a *Spot) OnReconnect() {
	a.mu.Lock()
	a.depthState = make(map[string][2][]ws.Level)
	a.bboState = make(map[string]spotBBO)
	a.mu.Unlock()
}
