// trades.go — Backpack perp public trade stream.
//
// Stream: trade.<SYMBOL>_USDC_PERP — per-fill events.
// Subscribe: {"method":"SUBSCRIBE","params":["trade.BTC_USDC_PERP",...]}
//
// Event wire:
//
//	{"stream":"trade.BTC_USDC_PERP",
//	 "data":{"e":"trade","E":...,"s":"BTC_USDC_PERP","p":"63125.5",
//	          "q":"0.001","b":"...","a":"...","t":...,"T":...,"m":true}}
//
// m=true → buyer was maker → taker SOLD → Sell tick.
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

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

const tradesWS = "wss://ws.backpack.exchange"
const marketsURL = "https://api.backpack.exchange/api/v1/markets"

// Cached valid PERP base-symbols, refreshed every hour. Without this,
// prewarm sends ~1000 random tokens at the WS, gets a flood of
// "Invalid market" errors, and may rate-limit the connection so the
// few valid SUBSCRIBE frames (BTC, ETH, SOL) never resolve.
var (
	validMu     sync.RWMutex
	validBases  map[string]struct{}
	validAt     time.Time
	validClient = &http.Client{Timeout: 6 * time.Second}
)

func refreshValidMarkets(ctx context.Context) {
	validMu.RLock()
	fresh := time.Since(validAt) < time.Hour && len(validBases) > 0
	validMu.RUnlock()
	if fresh {
		return
	}
	req, _ := http.NewRequestWithContext(ctx, "GET", marketsURL, nil)
	req.Header.Set("User-Agent", "Mozilla/5.0 avalant-fetcher/go")
	resp, err := validClient.Do(req)
	if err != nil {
		return
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	var doc []struct {
		Symbol     string `json:"symbol"`
		MarketType string `json:"marketType"`
	}
	if err := sonic.Unmarshal(body, &doc); err != nil {
		return
	}
	out := make(map[string]struct{}, 100)
	for _, m := range doc {
		if m.MarketType != "PERP" {
			continue
		}
		// Strip _USDC_PERP to leave bare base ("BTC", "SOL", ...).
		if base := strings.TrimSuffix(m.Symbol, "_USDC_PERP"); base != m.Symbol {
			out[strings.ToUpper(base)] = struct{}{}
		}
	}
	if len(out) == 0 {
		return
	}
	validMu.Lock()
	validBases = out
	validAt = time.Now()
	validMu.Unlock()
}

type Trades struct{}

func NewTrades(onTick ticks.UpdateFunc) *ticks.Runner {
	return ticks.NewRunner(&Trades{}, onTick)
}

func (a *Trades) Name() string                          { return "backpack" }
func (a *Trades) URL(_ context.Context) (string, error) { return tradesWS, nil }

func (a *Trades) BuildSubscribe(symbols []string) [][]byte {
	ctx, cancel := context.WithTimeout(context.Background(), 6*time.Second)
	defer cancel()
	refreshValidMarkets(ctx)
	validMu.RLock()
	known := validBases
	validMu.RUnlock()
	// Filter to only valid PERP bases. If REST refresh failed (empty
	// `known`), let everything through — better than zero subs on a
	// transient REST error.
	filtered := make([]string, 0, len(symbols))
	for _, s := range symbols {
		base := strings.ToUpper(s)
		if known != nil {
			if _, ok := known[base]; !ok {
				continue
			}
		}
		filtered = append(filtered, base)
	}
	if len(filtered) == 0 {
		return nil
	}
	params := make([]string, len(filtered))
	for i, s := range filtered {
		params[i] = "trade." + s + "_USDC_PERP"
	}
	frame := map[string]any{"method": "SUBSCRIBE", "params": params}
	b, _ := ticks.MarshalJSON(frame)
	return [][]byte{b}
}

func (a *Trades) Parse(frame []byte) ([]ticks.Tick, error) {
	// Same e/E case-insensitive collision handling as Binance — bind both.
	var wrap struct {
		Stream string `json:"stream"`
		Data   struct {
			EvType string `json:"e"`
			EvTime int64  `json:"E"`
			S      string `json:"s"`
			P      string `json:"p"`
			Q      string `json:"q"`
			Tid    int64  `json:"t"`
			TT     int64  `json:"T"`
			M      bool   `json:"m"`
		} `json:"data"`
	}
	_ = wrap.Data.EvTime
	if err := ticks.UnmarshalJSON(frame, &wrap); err != nil {
		return nil, err
	}
	if !strings.HasPrefix(wrap.Stream, "trade.") || wrap.Data.EvType != "trade" {
		return nil, nil
	}
	if !strings.HasSuffix(wrap.Data.S, "_USDC_PERP") {
		return nil, nil
	}
	token := strings.TrimSuffix(wrap.Data.S, "_USDC_PERP")
	price, _ := strconv.ParseFloat(wrap.Data.P, 64)
	size, _ := strconv.ParseFloat(wrap.Data.Q, 64)
	if price <= 0 || size <= 0 {
		return nil, nil
	}
	side := ticks.Buy
	if wrap.Data.M {
		side = ticks.Sell
	}
	return []ticks.Tick{{
		Exchange: "backpack",
		Symbol:   token,
		Price:    price,
		Size:     size,
		Side:     side,
		TsMS:     wrap.Data.TT,
		ID:       strconv.FormatInt(wrap.Data.Tid, 10),
	}}, nil
}

// Backpack — server pings every 60s; lib pings work.
func (a *Trades) Heartbeat() []byte                { return nil }
func (a *Trades) HeartbeatInterval() time.Duration { return 0 }
func (a *Trades) PongFor(_ []byte) []byte          { return nil }
func (a *Trades) UseLibPings() bool                { return true }
func (a *Trades) SubscribeDelay() time.Duration    { return 0 }
func (a *Trades) MaxSymbols() int                  { return 0 }
func (a *Trades) DecompressGzip() bool             { return false }

func (a *Trades) OnReconnect() {}
