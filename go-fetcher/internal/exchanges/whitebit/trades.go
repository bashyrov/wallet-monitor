// trades.go — WhiteBIT public trade stream.
//
// Method: trades_subscribe (NOT deals_subscribe — deals_* requires auth
// and silently rejects with code 6). Public on api.whitebit.com/ws.
//
// Subscribe: {"id":1,"method":"trades_subscribe","params":["BTC_PERP"]}
//
// Event wire:
//
//	{"method":"trades_update",
//	 "params":["BTC_PERP",[{"id":...,"time":<unix-float-sec>,
//	                       "price":"63125.5","amount":"0.001",
//	                       "type":"buy"|"sell"}]]}
package whitebit

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

const tradesWS = "wss://api.whitebit.com/ws"
const wbMarketsURL = "https://whitebit.com/api/v4/public/futures"

// Cache of valid PERP base symbols on WhiteBIT, refreshed hourly.
// Prevents the prewarm flood of "market does not exist" subscribe errors
// (any of which can rate-limit the conn and lose the valid subs).
var (
	wbValidMu     sync.RWMutex
	wbValidBases  map[string]struct{}
	wbValidAt     time.Time
	wbValidClient = &http.Client{Timeout: 6 * time.Second}
)

func wbRefreshValid(ctx context.Context) {
	wbValidMu.RLock()
	fresh := time.Since(wbValidAt) < time.Hour && len(wbValidBases) > 0
	wbValidMu.RUnlock()
	if fresh {
		return
	}
	req, _ := http.NewRequestWithContext(ctx, "GET", wbMarketsURL, nil)
	req.Header.Set("User-Agent", "Mozilla/5.0 avalant-fetcher/go")
	resp, err := wbValidClient.Do(req)
	if err != nil {
		return
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	var doc struct {
		Result []struct {
			TickerID string `json:"ticker_id"`
		} `json:"result"`
	}
	if err := sonic.Unmarshal(body, &doc); err != nil {
		return
	}
	out := make(map[string]struct{}, len(doc.Result))
	for _, m := range doc.Result {
		if base := strings.TrimSuffix(m.TickerID, "_PERP"); base != m.TickerID {
			out[strings.ToUpper(base)] = struct{}{}
		}
	}
	if len(out) == 0 {
		return
	}
	wbValidMu.Lock()
	wbValidBases = out
	wbValidAt = time.Now()
	wbValidMu.Unlock()
}

type Trades struct{}

func NewTrades(onTick ticks.UpdateFunc) *ticks.Runner {
	return ticks.NewRunner(&Trades{}, onTick)
}

func (a *Trades) Name() string                          { return "whitebit" }
func (a *Trades) URL(_ context.Context) (string, error) { return tradesWS, nil }

func (a *Trades) BuildSubscribe(symbols []string) [][]byte {
	ctx, cancel := context.WithTimeout(context.Background(), 6*time.Second)
	defer cancel()
	wbRefreshValid(ctx)
	wbValidMu.RLock()
	known := wbValidBases
	wbValidMu.RUnlock()
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
	frames := make([][]byte, 0, len(filtered))
	for i, s := range filtered {
		f := map[string]any{
			"id":     i + 1,
			"method": "trades_subscribe",
			"params": []string{s + "_PERP"},
		}
		b, _ := ticks.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

func (a *Trades) Parse(frame []byte) ([]ticks.Tick, error) {
	var msg struct {
		Method string `json:"method"`
		Params []any  `json:"params"`
	}
	if err := ticks.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Method != "trades_update" || len(msg.Params) < 2 {
		return nil, nil
	}
	market, _ := msg.Params[0].(string)
	if !strings.HasSuffix(market, "_PERP") {
		return nil, nil
	}
	token := strings.TrimSuffix(market, "_PERP")
	rows, ok := msg.Params[1].([]any)
	if !ok {
		return nil, nil
	}
	out := make([]ticks.Tick, 0, len(rows))
	for _, r := range rows {
		row, ok := r.(map[string]any)
		if !ok {
			continue
		}
		var price, amount float64
		switch p := row["price"].(type) {
		case string:
			price, _ = strconv.ParseFloat(p, 64)
		case float64:
			price = p
		}
		switch a := row["amount"].(type) {
		case string:
			amount, _ = strconv.ParseFloat(a, 64)
		case float64:
			amount = a
		}
		if price <= 0 || amount <= 0 {
			continue
		}
		side := ticks.Buy
		if t, ok := row["type"].(string); ok && t == "sell" {
			side = ticks.Sell
		}
		var tsMs int64
		switch tt := row["time"].(type) {
		case float64:
			// WhiteBIT timestamps are typically Unix seconds float
			tsMs = int64(tt * 1000)
		case int64:
			tsMs = tt
		}
		var tid string
		switch idv := row["id"].(type) {
		case string:
			tid = idv
		case float64:
			tid = strconv.FormatInt(int64(idv), 10)
		}
		out = append(out, ticks.Tick{
			Exchange: "whitebit",
			Symbol:   token,
			Price:    price,
			Size:     amount,
			Side:     side,
			TsMS:     tsMs,
			ID:       tid,
		})
	}
	if len(out) == 0 {
		return nil, nil
	}
	return out, nil
}

// WhiteBIT — server times out after 60s of inactivity; lib pings keep alive.
func (a *Trades) Heartbeat() []byte                { return nil }
func (a *Trades) HeartbeatInterval() time.Duration { return 0 }
func (a *Trades) PongFor(_ []byte) []byte          { return nil }
func (a *Trades) UseLibPings() bool                { return true }
func (a *Trades) SubscribeDelay() time.Duration    { return 0 }
func (a *Trades) MaxSymbols() int                  { return 0 }
func (a *Trades) DecompressGzip() bool             { return false }

func (a *Trades) OnReconnect() {}
