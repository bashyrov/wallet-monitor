// WhiteBIT spot orderbook WS.
//
// Same WS host as futures (api.whitebit.com/ws) and the same depth_subscribe
// protocol — the only difference is the market suffix: `BTC_USDT` for spot
// vs `BTC_PERP` for the futures product.
//
// Sharing Parse via embedding doesn't work cleanly here because Parse
// strips the "_PERP" suffix to extract the symbol, so we have a small
// Spot type with its own BuildSubscribe + market-suffix detection.
package whitebit

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

type Spot struct {
	store *cache.Store
	books map[string]*book
}

func NewSpot(store *cache.Store) *ws.Runner {
	a := &Spot{store: store, books: make(map[string]*book)}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("whitebit_spot", snap.Symbol, snap, "ws")
	})
}

func (a *Spot) Name() string                          { return "whitebit_spot" }
func (a *Spot) URL(_ context.Context) (string, error) { return futuresWS, nil }

func (a *Spot) BuildSubscribe(symbols []string) [][]byte {
	frames := make([][]byte, 0, len(symbols))
	for i, s := range symbols {
		params := []any{strings.ToUpper(s) + "_USDT", 100, "0", true}
		f := map[string]any{
			"id":     i + 1,
			"method": "depth_subscribe",
			"params": params,
		}
		b, _ := ws.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

func (a *Spot) Parse(frame []byte) (*ws.Snapshot, error) {
	var msg struct {
		Method string `json:"method"`
		Params []any  `json:"params"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Method != "depth_update" {
		return nil, nil
	}
	if len(msg.Params) < 3 {
		return nil, nil
	}
	isFull, _ := msg.Params[0].(bool)
	body, ok := msg.Params[1].(map[string]any)
	if !ok {
		return nil, nil
	}
	market, _ := msg.Params[2].(string)
	// WhiteBit shares one WS for spot and futures; we filter by suffix
	// so the spot adapter ignores _PERP frames and vice-versa.
	if !strings.HasSuffix(market, "_USDT") || strings.HasSuffix(market, "_PERP") {
		return nil, nil
	}
	token := strings.TrimSuffix(market, "_USDT")

	bk, ok := a.books[token]
	if !ok {
		bk = &book{bids: make(map[float64]float64), asks: make(map[float64]float64)}
		a.books[token] = bk
	}
	if isFull {
		bk.bids = make(map[float64]float64)
		bk.asks = make(map[float64]float64)
	}
	apply := func(side map[float64]float64, key string) {
		raw, ok := body[key].([]any)
		if !ok {
			return
		}
		for _, lvl := range raw {
			pair, ok := lvl.([]any)
			if !ok || len(pair) < 2 {
				continue
			}
			pxStr, _ := pair[0].(string)
			szStr, _ := pair[1].(string)
			px, perr := strconv.ParseFloat(pxStr, 64)
			sz, serr := strconv.ParseFloat(szStr, 64)
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
	apply(bk.bids, "bids")
	apply(bk.asks, "asks")

	return &ws.Snapshot{
		Symbol: token,
		Bids:   ws.SortedLevels(bk.bids, ws.Bids, 200),
		Asks:   ws.SortedLevels(bk.asks, ws.Asks, 200),
	}, nil
}

func (a *Spot) Heartbeat() []byte                { return nil }
func (a *Spot) HeartbeatInterval() time.Duration { return 0 }
func (a *Spot) PongFor(_ []byte) []byte          { return nil }
func (a *Spot) UseLibPings() bool                { return true }
func (a *Spot) SubscribeDelay() time.Duration    { return 0 }
func (a *Spot) MaxSymbols() int                  { return 0 }
func (a *Spot) DecompressGzip() bool             { return false }
func (a *Spot) OnReconnect()                     { a.books = make(map[string]*book) }
