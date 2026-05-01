// Hyperliquid spot orderbook WS.
//
// Same WS host as futures (api.hyperliquid.xyz/ws) but spot pairs are
// addressed by a numeric `@<index>` ID rather than the bare token
// symbol. The index → symbol map comes from `/info {"type":"spotMeta"}`
// (POST). Each universe entry there has:
//
//	{"name":"HYPE/USDC", "index":107, "tokens":[150,0], ...}
//
// Subscribe coin = "@107" → response `coin` field also "@107"; we
// translate both directions via the in-memory map.
//
// Refresh: spot universe changes when HL lists a new token. 30-min TTL
// is generous enough that we don't slam /info under load but new
// listings show up within half an hour.
package hyperliquid

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bytedance/sonic"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const (
	spotInfoURL = "https://api.hyperliquid.xyz/info"
	spotMetaTTL = 30 * time.Minute
)

type spotMetaCache struct {
	mu          sync.RWMutex
	bySymbol    map[string]string // "HYPE" -> "@107"
	byIndex     map[string]string // "@107" -> "HYPE"
	lastRefresh time.Time
}

var spotMeta = &spotMetaCache{
	bySymbol: make(map[string]string),
	byIndex:  make(map[string]string),
}

// Refresh pulls /info spotMeta and rebuilds both maps. Best-effort —
// errors keep the previous map intact so a transient HL hiccup doesn't
// blank the spot adapter.
func (c *spotMetaCache) Refresh(ctx context.Context) error {
	c.mu.RLock()
	if time.Since(c.lastRefresh) < spotMetaTTL && len(c.bySymbol) > 0 {
		c.mu.RUnlock()
		return nil
	}
	c.mu.RUnlock()

	body := []byte(`{"type":"spotMeta"}`)
	req, err := http.NewRequestWithContext(ctx, "POST", spotInfoURL, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", "Mozilla/5.0 avalant-fetcher/go")
	cl := &http.Client{Timeout: 8 * time.Second}
	resp, err := cl.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		return err
	}
	if resp.StatusCode != 200 {
		return errors.New("hl spotMeta status " + strconv.Itoa(resp.StatusCode))
	}
	var doc struct {
		Universe []struct {
			Name  string `json:"name"`
			Index int    `json:"index"`
		} `json:"universe"`
	}
	if err := sonic.Unmarshal(raw, &doc); err != nil {
		return err
	}
	bySym := make(map[string]string, len(doc.Universe))
	byIdx := make(map[string]string, len(doc.Universe))
	for _, u := range doc.Universe {
		// Only `<TOKEN>/USDC` pairs map cleanly to the screener's
		// "<TOKEN>" symbol form. HL may at some point list non-USDC
		// pairs; we ignore those — they wouldn't intersect anything
		// the funding store knows about anyway.
		if !strings.HasSuffix(u.Name, "/USDC") {
			continue
		}
		token := strings.ToUpper(strings.TrimSuffix(u.Name, "/USDC"))
		coin := "@" + strconv.Itoa(u.Index)
		bySym[token] = coin
		byIdx[coin] = token
	}
	if len(bySym) == 0 {
		return errors.New("hl spotMeta: empty universe")
	}
	c.mu.Lock()
	c.bySymbol = bySym
	c.byIndex = byIdx
	c.lastRefresh = time.Now()
	c.mu.Unlock()
	log.L().Info().Int("pairs", len(bySym)).Msg("hyperliquid spotMeta refreshed")
	return nil
}

func (c *spotMetaCache) coinFor(token string) (string, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	v, ok := c.bySymbol[strings.ToUpper(token)]
	return v, ok
}

func (c *spotMetaCache) tokenFor(coin string) (string, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	v, ok := c.byIndex[coin]
	return v, ok
}

type Spot struct {
	store *cache.Store
}

func NewSpot(store *cache.Store) *ws.Runner {
	a := &Spot{store: store}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("hyperliquid_spot", snap.Symbol, snap, "ws")
	})
}

func (a *Spot) Name() string                          { return "hyperliquid_spot" }
func (a *Spot) URL(_ context.Context) (string, error) { return futuresWS, nil }

func (a *Spot) BuildSubscribe(symbols []string) [][]byte {
	// Synchronous refresh so the very first subscribe has the index map.
	// Errors are non-fatal — we'll skip symbols we can't translate yet
	// and rely on the next reconnect to pick them up.
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := spotMeta.Refresh(ctx); err != nil {
		log.L().Warn().Err(err).Msg("hl spotMeta refresh failed — proceeding with stale map")
	}

	frames := make([][]byte, 0, len(symbols))
	for _, s := range symbols {
		coin, ok := spotMeta.coinFor(s)
		if !ok {
			continue // not a spot pair on HL — silently skip
		}
		f := map[string]any{
			"method":       "subscribe",
			"subscription": map[string]any{"type": "l2Book", "coin": coin},
		}
		b, _ := ws.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

func (a *Spot) Parse(frame []byte) (*ws.Snapshot, error) {
	var msg struct {
		Channel string `json:"channel"`
		Data    struct {
			Coin   string `json:"coin"`
			Levels [2][]struct {
				Px string `json:"px"`
				Sz string `json:"sz"`
				N  int    `json:"n"`
			} `json:"levels"`
		} `json:"data"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Channel != "l2Book" {
		return nil, nil
	}
	// Spot frames carry coin = "@<idx>". The futures adapter sees the
	// same channel name with bare-token coins, so the @-prefix is what
	// distinguishes us from cross-leak.
	if !strings.HasPrefix(msg.Data.Coin, "@") {
		return nil, nil
	}
	token, ok := spotMeta.tokenFor(msg.Data.Coin)
	if !ok {
		return nil, nil
	}
	parseSide := func(rows []struct {
		Px string `json:"px"`
		Sz string `json:"sz"`
		N  int    `json:"n"`
	}) []ws.Level {
		out := make([]ws.Level, 0, len(rows))
		for _, r := range rows {
			px, _ := strconv.ParseFloat(r.Px, 64)
			sz, _ := strconv.ParseFloat(r.Sz, 64)
			if sz > 0 {
				out = append(out, ws.Level{px, sz})
			}
		}
		return out
	}
	return &ws.Snapshot{
		Symbol: token,
		Bids:   parseSide(msg.Data.Levels[0]),
		Asks:   parseSide(msg.Data.Levels[1]),
	}, nil
}

func (a *Spot) Heartbeat() []byte                { return nil }
func (a *Spot) HeartbeatInterval() time.Duration { return 0 }
func (a *Spot) PongFor(_ []byte) []byte          { return nil }
func (a *Spot) UseLibPings() bool                { return true }
func (a *Spot) SubscribeDelay() time.Duration    { return 500 * time.Millisecond } // same as futures (bug #?)
func (a *Spot) MaxSymbols() int                  { return 0 }
func (a *Spot) DecompressGzip() bool             { return false }
func (a *Spot) OnReconnect()                     {}
