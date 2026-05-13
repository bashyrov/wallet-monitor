// Package kucoin — KuCoin futures (USDT-M perp).
//
// Phase 2f migration: `level2Depth50` (100ms aggregated snapshot) →
// `/contractMarket/level2:<TOKEN>USDTM` raw tick-by-tick incremental.
//
// URL: dynamically fetched (see auth.go bullet-public flow).
// Subscribe:
//   {"id":N,"type":"subscribe","topic":"/contractMarket/level2:XBTUSDTM",
//    "privateChannel":false,"response":true}
//
// Inbound delta:
//   {"type":"message","topic":"/contractMarket/level2:XBTUSDTM",
//    "subject":"level2",
//    "data":{"sequence":N,"change":"price,side,size","timestamp":T}}
//
// `change` is a CSV: "<price>,<side>,<size>" with side ∈ {buy, sell}.
// size=0 ⇒ delete the level.
//
// Bootstrap (per symbol):
//   1. WS subscribes; deltas start arriving and queue in book.buffer.
//   2. seedREST() runs async — GET /api/v1/level2/snapshot?symbol=…
//      → {"data":{"sequence":N, "bids":[...], "asks":[...]}}
//   3. Set baseSeq=N, populate maps. Apply only buffered events where
//      sequence > baseSeq.
//   4. Steady state: deltas must be strictly increasing sequence —
//      gap detected resets state + kicks a re-seed.
//
// QUIRKS (carried over from `level2Depth50` adapter):
//   - URL needs token + connectId from POST (auth.go) — bug #17
//   - Subscribe rate limit ~3 msg/sec/conn → SubscribeDelay 350ms
//   - 30 symbols per conn (server resets ~99th subscribe; tighter than
//     advertised 100)
//   - App-level "ping" every 15s
//   - Symbol form: <TOKEN>USDTM with XBT alias for BTC
package kucoin

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

// Overridable in tests.
var restSnapshot = "https://api-futures.kucoin.com/api/v1/level2/snapshot?symbol=%s"

type Futures struct {
	store *cache.Store
	auth  *authClient
	mu    sync.Mutex
	books map[string]*book
}

type book struct {
	bids    map[float64]float64
	asks    map[float64]float64
	baseSeq uint64
	lastSeq uint64
	seeded  bool
	buffer  []bufferedDelta
}

type bufferedDelta struct {
	seq  uint64
	px   float64
	side string
	sz   float64
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{store: store, auth: &authClient{}, books: make(map[string]*book)}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("kucoin", snap.Symbol, snap, "ws")
	})
}

func (a *Futures) Name() string { return "kucoin" }

func (a *Futures) URL(ctx context.Context) (string, error) {
	u, _, err := a.auth.FetchURL(ctx)
	return u, err
}

// tokenToContract / contractToToken — KuCoin uses XBT for BTC.
func tokenToContract(s string) string {
	t := strings.ToUpper(s)
	if t == "BTC" {
		t = "XBT"
	}
	return t + "USDTM"
}

func contractToToken(c string) string {
	t := strings.TrimSuffix(c, "USDTM")
	if t == "XBT" {
		t = "BTC"
	}
	return t
}

func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
	frames := make([][]byte, 0, len(symbols))
	for i, s := range symbols {
		go a.seedREST(s)
		contract := tokenToContract(s)
		f := map[string]any{
			"id":             time.Now().UnixNano() + int64(i),
			"type":           "subscribe",
			"topic":          "/contractMarket/level2:" + contract,
			"privateChannel": false,
			"response":       true,
		}
		b, _ := ws.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

// seedREST fetches the L2 snapshot for one symbol and merges it into the
// per-symbol book. Async — does not block subscribe.
func (a *Futures) seedREST(symbol string) {
	contract := tokenToContract(symbol)
	token := contractToToken(contract)

	url := fmt.Sprintf(restSnapshot, contract)
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
		Data struct {
			Sequence anyNum     `json:"sequence"`
			Bids     [][]string `json:"bids"`
			Asks     [][]string `json:"asks"`
		} `json:"data"`
	}
	if err := sonic.Unmarshal(body, &doc); err != nil {
		return
	}
	seq := doc.Data.Sequence.Uint64()
	if seq == 0 {
		return
	}

	a.mu.Lock()
	defer a.mu.Unlock()
	bk := a.bookFor(token)
	bk.bids = make(map[float64]float64, len(doc.Data.Bids))
	bk.asks = make(map[float64]float64, len(doc.Data.Asks))
	for _, r := range doc.Data.Bids {
		if len(r) < 2 {
			continue
		}
		px, _ := strconv.ParseFloat(r[0], 64)
		sz, _ := strconv.ParseFloat(r[1], 64)
		if sz > 0 {
			bk.bids[px] = sz
		}
	}
	for _, r := range doc.Data.Asks {
		if len(r) < 2 {
			continue
		}
		px, _ := strconv.ParseFloat(r[0], 64)
		sz, _ := strconv.ParseFloat(r[1], 64)
		if sz > 0 {
			bk.asks[px] = sz
		}
	}
	bk.baseSeq = seq
	a.drainBuffer(bk)
}

// drainBuffer applies buffered deltas after a successful REST seed.
// Events with seq <= baseSeq are stale (already in snapshot) → drop.
// First applied event must satisfy seq == baseSeq+1 (strict contiguity);
// otherwise gap at bootstrap edge → reset to await a new seed.
func (a *Futures) drainBuffer(bk *book) {
	if !bk.seeded {
		for _, ev := range bk.buffer {
			if ev.seq <= bk.baseSeq {
				continue
			}
			if ev.seq != bk.baseSeq+1 {
				bk.buffer = nil
				bk.baseSeq = 0
				return
			}
			a.applyChange(bk, ev.px, ev.side, ev.sz)
			bk.lastSeq = ev.seq
			bk.seeded = true
		}
	}
	if bk.seeded {
		for _, ev := range bk.buffer {
			if ev.seq <= bk.lastSeq {
				continue
			}
			if ev.seq != bk.lastSeq+1 {
				bk.bids = make(map[float64]float64)
				bk.asks = make(map[float64]float64)
				bk.baseSeq = 0
				bk.lastSeq = 0
				bk.seeded = false
				bk.buffer = nil
				return
			}
			a.applyChange(bk, ev.px, ev.side, ev.sz)
			bk.lastSeq = ev.seq
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

func (a *Futures) applyChange(bk *book, px float64, side string, sz float64) {
	switch side {
	case "buy":
		if sz == 0 {
			delete(bk.bids, px)
		} else {
			bk.bids[px] = sz
		}
	case "sell":
		if sz == 0 {
			delete(bk.asks, px)
		} else {
			bk.asks[px] = sz
		}
	}
}

func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
	var msg struct {
		Type    string `json:"type"`
		Topic   string `json:"topic"`
		Subject string `json:"subject"`
		Data    struct {
			Sequence  anyNum `json:"sequence"`
			Change    string `json:"change"`
			Timestamp int64  `json:"timestamp"`
		} `json:"data"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Type != "message" || !strings.HasPrefix(msg.Topic, "/contractMarket/level2:") {
		return nil, nil
	}
	contract := strings.TrimPrefix(msg.Topic, "/contractMarket/level2:")
	if !strings.HasSuffix(contract, "USDTM") {
		return nil, nil
	}
	token := contractToToken(contract)

	parts := strings.Split(msg.Data.Change, ",")
	if len(parts) != 3 {
		return nil, nil
	}
	px, errP := strconv.ParseFloat(parts[0], 64)
	side := parts[1]
	sz, errS := strconv.ParseFloat(parts[2], 64)
	if errP != nil || errS != nil {
		return nil, nil
	}
	seq := msg.Data.Sequence.Uint64()
	if seq == 0 {
		return nil, nil
	}

	a.mu.Lock()
	defer a.mu.Unlock()
	bk := a.bookFor(token)

	// Pre-snapshot: buffer the delta.
	if bk.baseSeq == 0 {
		bk.buffer = append(bk.buffer, bufferedDelta{seq, px, side, sz})
		return nil, nil
	}

	// Snapshot landed but no delta applied yet — first must be baseSeq+1.
	if !bk.seeded {
		if seq <= bk.baseSeq {
			return nil, nil
		}
		if seq != bk.baseSeq+1 {
			bk.baseSeq = 0
			return nil, nil
		}
		a.applyChange(bk, px, side, sz)
		bk.lastSeq = seq
		bk.seeded = true
	} else {
		// Steady state: strict +1 contiguity.
		if seq != bk.lastSeq+1 {
			bk.bids = make(map[float64]float64)
			bk.asks = make(map[float64]float64)
			bk.baseSeq = 0
			bk.lastSeq = 0
			bk.seeded = false
			go a.seedREST(token)
			return nil, nil
		}
		a.applyChange(bk, px, side, sz)
		bk.lastSeq = seq
	}

	return &ws.Snapshot{
		Symbol: token,
		Bids:   ws.SortedLevels(bk.bids, ws.Bids, 200),
		Asks:   ws.SortedLevels(bk.asks, ws.Asks, 200),
	}, nil
}

func (a *Futures) Heartbeat() []byte {
	frame, _ := ws.MarshalJSON(map[string]any{"id": time.Now().UnixNano(), "type": "ping"})
	return frame
}
func (a *Futures) HeartbeatInterval() time.Duration { return 15 * time.Second }
func (a *Futures) PongFor(_ []byte) []byte          { return nil }
func (a *Futures) UseLibPings() bool                { return false }
func (a *Futures) SubscribeDelay() time.Duration    { return 350 * time.Millisecond }
func (a *Futures) MaxSymbols() int                  { return 30 }
func (a *Futures) DecompressGzip() bool             { return false }

func (a *Futures) OnReconnect() {
	a.mu.Lock()
	a.books = make(map[string]*book)
	a.mu.Unlock()
}

// anyNum accepts uint64 either as a JSON number or as a string —
// KuCoin's REST returns "sequence":"123" while WS returns "sequence":123.
type anyNum struct {
	v uint64
}

func (n *anyNum) UnmarshalJSON(b []byte) error {
	s := strings.Trim(string(b), `"`)
	if s == "" || s == "null" {
		return nil
	}
	u, err := strconv.ParseUint(s, 10, 64)
	if err != nil {
		return nil // soft-fail — keep zero
	}
	n.v = u
	return nil
}

func (n anyNum) Uint64() uint64 { return n.v }
