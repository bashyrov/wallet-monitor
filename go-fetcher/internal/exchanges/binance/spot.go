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
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

// Combined-stream base — bare endpoint, no query params. All stream
// subscriptions go through SUBSCRIBE frames only (no URL-based streams).
// This avoids double-subscription (URL streams + SUBSCRIBE = 2× the
// same streams), which was causing Binance to close with 1008.
const spotCombinedBase = "wss://stream.binance.com:9443/stream"

type Spot struct {
	store *cache.Store
}

func NewSpot(store *cache.Store) *ws.Runner {
	a := &Spot{store: store}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("binance_spot", snap.Symbol, snap, "ws")
	})
}

func (a *Spot) Name() string { return "binance_spot" }

// URL — always the bare combined-stream endpoint. Streams are attached
// via SUBSCRIBE frames in BuildSubscribe, not baked into the URL.
// Previously we embedded streams in the URL AND sent a SUBSCRIBE frame,
// resulting in double-subscription (2× the same streams) which caused
// Binance to close with 1008 policy violation.
func (a *Spot) URL(_ context.Context) (string, error) {
	return spotCombinedBase, nil
}

// BuildSubscribe sends SUBSCRIBE frames only — no URL-based stream
// selection. Binance combined-stream accepts SUBSCRIBE on the bare
// /stream endpoint.
func (a *Spot) BuildSubscribe(symbols []string) [][]byte {
	if len(symbols) == 0 {
		return nil
	}
	// Binance public WS rejects oversized SUBSCRIBE frames; 200 per chunk.
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
