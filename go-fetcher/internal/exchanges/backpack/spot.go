// Backpack spot orderbook WS.
//
// Same WS host (ws.backpack.exchange) and DELTA stream protocol as
// futures. The only difference is the symbol form: `<BASE>_USDC` for
// spot vs `<BASE>_USDC_PERP` for the perp product.
//
// REST seed for the initial snapshot uses /api/v1/depth?symbol=<BASE>_USDC.
package backpack

import (
	"context"
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

const restDepthSpot = "https://api.backpack.exchange/api/v1/depth?symbol=%s_USDC"

type Spot struct {
	store *cache.Store
	mu    sync.Mutex
	books map[string]*book
}

func NewSpot(store *cache.Store) *ws.Runner {
	a := &Spot{store: store, books: make(map[string]*book)}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("backpack_spot", snap.Symbol, snap, "ws")
	})
}

func (a *Spot) Name() string                          { return "backpack_spot" }
func (a *Spot) URL(_ context.Context) (string, error) { return futuresWS, nil }

func (a *Spot) BuildSubscribe(symbols []string) [][]byte {
	for _, s := range symbols {
		go a.seedRest(s)
	}
	params := make([]string, len(symbols))
	for i, s := range symbols {
		params[i] = "depth." + strings.ToUpper(s) + "_USDC"
	}
	frame := map[string]any{"method": "SUBSCRIBE", "params": params}
	b, _ := ws.MarshalJSON(frame)
	return [][]byte{b}
}

func (a *Spot) seedRest(symbol string) {
	url := strings.ReplaceAll(restDepthSpot, "%s", strings.ToUpper(symbol))
	cl := &http.Client{Timeout: 6 * time.Second}
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return
	}
	req.Header.Set("User-Agent", "Mozilla/5.0 avalant-fetcher/go")
	resp, err := cl.Do(req)
	if err != nil {
		return
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return
	}
	var doc struct {
		Bids [][]string `json:"bids"`
		Asks [][]string `json:"asks"`
	}
	if err := sonic.Unmarshal(body, &doc); err != nil {
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	bk, ok := a.books[symbol]
	if !ok {
		bk = &book{bids: make(map[float64]float64), asks: make(map[float64]float64)}
		a.books[symbol] = bk
	}
	for _, r := range doc.Bids {
		if len(r) >= 2 {
			px, _ := strconv.ParseFloat(r[0], 64)
			sz, _ := strconv.ParseFloat(r[1], 64)
			if sz > 0 {
				bk.bids[px] = sz
			}
		}
	}
	for _, r := range doc.Asks {
		if len(r) >= 2 {
			px, _ := strconv.ParseFloat(r[0], 64)
			sz, _ := strconv.ParseFloat(r[1], 64)
			if sz > 0 {
				bk.asks[px] = sz
			}
		}
	}
	bk.seeded = true
}

func (a *Spot) Parse(frame []byte) (*ws.Snapshot, error) {
	var msg struct {
		Stream string `json:"stream"`
		Data   struct {
			Symbol string     `json:"s"`
			Bids   [][]string `json:"b"`
			Asks   [][]string `json:"a"`
		} `json:"data"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if !strings.HasPrefix(msg.Stream, "depth.") {
		return nil, nil
	}
	sym := msg.Data.Symbol
	// Spot symbols end in _USDC but not _USDC_PERP — filter so the spot
	// adapter ignores any cross-leak from the futures stream.
	if !strings.HasSuffix(sym, "_USDC") || strings.HasSuffix(sym, "_USDC_PERP") {
		return nil, nil
	}
	token := strings.TrimSuffix(sym, "_USDC")

	a.mu.Lock()
	bk, ok := a.books[token]
	if !ok {
		bk = &book{bids: make(map[float64]float64), asks: make(map[float64]float64)}
		a.books[token] = bk
	}
	apply := func(side map[float64]float64, rows [][]string) {
		for _, r := range rows {
			if len(r) < 2 {
				continue
			}
			px, _ := strconv.ParseFloat(r[0], 64)
			sz, _ := strconv.ParseFloat(r[1], 64)
			if sz == 0 {
				delete(side, px)
			} else {
				side[px] = sz
			}
		}
	}
	apply(bk.bids, msg.Data.Bids)
	apply(bk.asks, msg.Data.Asks)
	bids := ws.SortedLevels(bk.bids, ws.Bids, 200)
	asks := ws.SortedLevels(bk.asks, ws.Asks, 200)
	a.mu.Unlock()

	return &ws.Snapshot{Symbol: token, Bids: bids, Asks: asks}, nil
}

func (a *Spot) Heartbeat() []byte                { return nil }
func (a *Spot) HeartbeatInterval() time.Duration { return 0 }
func (a *Spot) PongFor(_ []byte) []byte          { return nil }
func (a *Spot) UseLibPings() bool                { return true }
func (a *Spot) SubscribeDelay() time.Duration    { return 0 }
func (a *Spot) MaxSymbols() int                  { return 0 }
func (a *Spot) DecompressGzip() bool             { return false }
func (a *Spot) OnReconnect() {
	a.mu.Lock()
	a.books = make(map[string]*book)
	a.mu.Unlock()
}
