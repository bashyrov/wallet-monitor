// Package kucoin — KuCoin futures (USDT-M perp).
//
// Channel: /contractMarket/level2Depth5:<TOKEN>USDTM
//
// Snapshot-only: exchange pushes a full 5-level book on every BBO change.
// No delta-merge, no REST seed, no sequence tracking — just parse and emit.
//
// Subscribe (one frame per symbol, 350ms delay for server rate limit):
//
//	{"id":N,"type":"subscribe","topic":"/contractMarket/level2Depth5:XBTUSDTM",
//	 "privateChannel":false,"response":true}
//
// Inbound frame:
//
//	{"type":"message","topic":"/contractMarket/level2Depth5:XBTUSDTM",
//	 "subject":"level2Depth5Snapshot",
//	 "data":{"bids":[["px","sz"]...],"asks":[["px","sz"]...],"ts":N,"sequence":N}}
//
// Bids are pre-sorted descending (best first), asks ascending — no re-sort
// needed. Up to 5 levels per side.
//
// QUIRKS:
//   - URL needs bullet token from POST (auth.go)
//   - Subscribe rate limit ~3 msg/sec/conn → SubscribeDelay 350ms
//   - MaxSymbols=50 per connection (server tolerates ~100; 50 is safe)
//   - App-level "ping" every 15s required
//   - Symbol form: <TOKEN>USDTM with XBT alias for BTC
package kucoin

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

type Futures struct {
	store *cache.Store
	auth  *authClient
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{store: store, auth: &authClient{}}
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
		contract := tokenToContract(s)
		f := map[string]any{
			"id":             time.Now().UnixNano() + int64(i),
			"type":           "subscribe",
			"topic":          "/contractMarket/level2Depth5:" + contract,
			"privateChannel": false,
			"response":       true,
		}
		b, _ := ws.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
	// KuCoin level2Depth5 sends bids/asks as [price_string, size_number, ...].
	// The size element is a JSON number (not string), so [][]string would fail
	// to unmarshal. Use []interface{} per row and handle both float64 and string.
	var msg struct {
		Type  string `json:"type"`
		Topic string `json:"topic"`
		Data  struct {
			Bids [][]interface{} `json:"bids"`
			Asks [][]interface{} `json:"asks"`
			Ts   int64           `json:"ts"`
		} `json:"data"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	const topicPrefix = "/contractMarket/level2Depth5:"
	if msg.Type != "message" || !strings.HasPrefix(msg.Topic, topicPrefix) {
		return nil, nil
	}
	contract := strings.TrimPrefix(msg.Topic, topicPrefix)
	if !strings.HasSuffix(contract, "USDTM") {
		return nil, nil
	}
	token := contractToToken(contract)

	toFloat := func(v interface{}) float64 {
		switch t := v.(type) {
		case float64:
			return t
		case string:
			f, _ := strconv.ParseFloat(t, 64)
			return f
		}
		return 0
	}

	parseLevels := func(rows [][]interface{}) []ws.Level {
		out := make([]ws.Level, 0, len(rows))
		for _, r := range rows {
			if len(r) < 2 {
				continue
			}
			px := toFloat(r[0])
			sz := toFloat(r[1])
			if sz <= 0 {
				continue
			}
			out = append(out, ws.Level{px, sz})
		}
		return out
	}

	var evt time.Time
	if msg.Data.Ts > 0 {
		evt = time.UnixMilli(msg.Data.Ts)
	}
	return &ws.Snapshot{
		Symbol:    token,
		Bids:      parseLevels(msg.Data.Bids),
		Asks:      parseLevels(msg.Data.Asks),
		EventTime: evt,
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
func (a *Futures) MaxSymbols() int                  { return 50 }
func (a *Futures) DecompressGzip() bool             { return false }
func (a *Futures) OnReconnect()                     {}
