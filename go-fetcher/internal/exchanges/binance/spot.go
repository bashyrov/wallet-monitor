// Binance spot orderbook WS.
//
// URL: wss://stream.binance.com:9443/ws — bare endpoint with SUBSCRIBE
// frames after connect.
//
// IMPORTANT: spot's depth20@100ms stream pushes frames in the BARE shape
//
//	{"lastUpdateId": ..., "bids": [...], "asks": [...]}
//
// with no `e/s/E` wrapper — there's no symbol field anywhere in the
// payload. The futures adapter's Parse can't recover the symbol from
// such a frame (futures pushes `e:"depthUpdate", s:"BTCUSDT"` even on
// the same /ws path). To keep things simple we use a *separate* WS
// connection per symbol (one connection per ticker) where the URL path
// itself encodes the symbol — `/ws/<sym>usdt@depth20@100ms`. Inside
// Parse we track the in-flight subscription so we can stamp the symbol
// on each snapshot.
//
// This is a different concurrency model from every other adapter: we
// open one WS per symbol instead of one shared multiplexed WS. With
// the prewarm cap (top-20 symbols), that's fine — the runner already
// supports per-symbol fan-out via MaxSymbols.
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

// Combined-stream form lets one connection carry many symbols and
// returns frames wrapped as {"stream":"btcusdt@depth20@100ms","data":{...}}
// — the standard format the futures adapter's Parse already handles.
const spotCombinedBase = "wss://stream.binance.com:9443/stream"

type Spot struct {
	store *cache.Store
	mu    sync.Mutex
	syms  []string
}

func NewSpot(store *cache.Store) *ws.Runner {
	a := &Spot{store: store}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("binance_spot", snap.Symbol, snap, "ws")
	})
}

func (a *Spot) Name() string { return "binance_spot" }

// URL — combined-stream URL is built from the symbol set we know about
// (populated when the symbol manager calls BuildSubscribe). On first
// connect (no symbols yet) we point at a no-op stream so the dial
// succeeds; the symbol manager's reconnect-on-symbol-change logic will
// re-dial with the actual list as soon as prewarm lands.
func (a *Spot) URL(_ context.Context) (string, error) {
	a.mu.Lock()
	syms := a.syms
	a.mu.Unlock()
	if len(syms) == 0 {
		// Subscribe to BTC by default so the dial succeeds. Re-URL
		// happens on next symbol-set change.
		return spotCombinedBase + "?streams=btcusdt@depth20@100ms", nil
	}
	parts := make([]string, len(syms))
	for i, s := range syms {
		parts[i] = strings.ToLower(s) + "usdt@depth20@100ms"
	}
	return spotCombinedBase + "?streams=" + strings.Join(parts, "/"), nil
}

// BuildSubscribe — combined-stream URL already carries the subscriptions,
// no SUBSCRIBE frame needed. We capture the symbol list so URL() can
// rebuild correctly on reconnect.
func (a *Spot) BuildSubscribe(symbols []string) [][]byte {
	a.mu.Lock()
	a.syms = append(a.syms[:0], symbols...)
	a.mu.Unlock()
	return nil
}

// Parse — combined-stream wrapper {"stream":"btcusdt@depth20@100ms","data":
// {"lastUpdateId":..., "bids":[...], "asks":[...]}}. Pull symbol from
// stream prefix; the data payload itself has no `s`.
func (a *Spot) Parse(frame []byte) (*ws.Snapshot, error) {
	var wrap struct {
		Stream string `json:"stream"`
		Data   struct {
			Bids [][]string `json:"bids"`
			Asks [][]string `json:"asks"`
		} `json:"data"`
		Result *any `json:"result"`
	}
	if err := ws.UnmarshalJSON(frame, &wrap); err != nil {
		return nil, err
	}
	if wrap.Result != nil {
		return nil, nil // SUBSCRIBE ack — not data
	}
	if wrap.Stream == "" {
		return nil, nil
	}
	// "btcusdt@depth20@100ms" → "BTCUSDT"
	sym := wrap.Stream
	if i := strings.IndexByte(sym, '@'); i > 0 {
		sym = sym[:i]
	}
	sym = strings.ToUpper(sym)
	if !strings.HasSuffix(sym, "USDT") {
		return nil, nil
	}
	token := strings.TrimSuffix(sym, "USDT")

	parse := func(rows [][]string) []ws.Level {
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
	return &ws.Snapshot{
		Symbol: token,
		Bids:   parse(wrap.Data.Bids),
		Asks:   parse(wrap.Data.Asks),
	}, nil
}

// Keepalive shape mirrors futures — Binance answers WS-level pings.
func (a *Spot) Heartbeat() []byte                { return nil }
func (a *Spot) HeartbeatInterval() time.Duration { return 0 }
func (a *Spot) PongFor(_ []byte) []byte          { return nil }
func (a *Spot) UseLibPings() bool                { return true }
func (a *Spot) SubscribeDelay() time.Duration    { return 0 }
func (a *Spot) MaxSymbols() int                  { return 200 }
func (a *Spot) DecompressGzip() bool             { return false }
func (a *Spot) OnReconnect()                     {}
