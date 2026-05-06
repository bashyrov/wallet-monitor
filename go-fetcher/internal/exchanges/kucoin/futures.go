// Package kucoin — KuCoin futures (USDT-M perp).
//
// URL: dynamically fetched (see auth.go bullet-public flow).
// Subscribe: {"id":1,"type":"subscribe","topic":"/contractMarket/level2:XBTUSDTM,...","privateChannel":false,"response":true}
//
// QUIRKS:
//   - URL needs token + connectId fetched via POST (auth.go) — bug #17
//   - Subscribe rate-limited to ~3/sec/conn → SubscribeDelay 400ms (bug #19)
//   - App-level "ping": {"id":1,"type":"ping"} → server replies
//     {"id":"1","type":"pong"} every interval-from-bullet (default 18s)
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
	books map[string]*book
}

type book struct {
	bids map[float64]float64
	asks map[float64]float64
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

// kucoinBatch — symbols per subscribe frame. KuCoin supports comma-separated
// topics; batching cuts frame count 10× (100 syms → 10 frames) and stays
// well under any per-connection subscribe rate limit.
const kucoinBatch = 10

func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
	topics := make([]string, len(symbols))
	for i, s := range symbols {
		token := strings.ToUpper(s)
		if token == "BTC" {
			token = "XBT"
		}
		topics[i] = token + "USDTM"
	}
	frames := make([][]byte, 0, (len(topics)+kucoinBatch-1)/kucoinBatch)
	for i := 0; i < len(topics); i += kucoinBatch {
		end := i + kucoinBatch
		if end > len(topics) {
			end = len(topics)
		}
		f := map[string]any{
			"id":             time.Now().UnixNano() + int64(i),
			"type":           "subscribe",
			"topic":          "/contractMarket/level2Depth50:" + strings.Join(topics[i:end], ","),
			"privateChannel": false,
			"response":       true,
		}
		b, _ := ws.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
	var msg struct {
		Type    string `json:"type"`
		Topic   string `json:"topic"`
		Subject string `json:"subject"`
		Data    struct {
			Bids [][]any `json:"bids"`
			Asks [][]any `json:"asks"`
		} `json:"data"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Type != "message" || !strings.HasPrefix(msg.Topic, "/contractMarket/level2Depth50:") {
		return nil, nil
	}
	contract := strings.TrimPrefix(msg.Topic, "/contractMarket/level2Depth50:")
	if !strings.HasSuffix(contract, "USDTM") {
		return nil, nil
	}
	token := strings.TrimSuffix(contract, "USDTM")
	if token == "XBT" {
		token = "BTC"
	}

	parseSide := func(rows [][]any) []ws.Level {
		out := make([]ws.Level, 0, len(rows))
		for _, r := range rows {
			if len(r) < 2 {
				continue
			}
			var px, sz float64
			switch v := r[0].(type) {
			case string:
				px, _ = strconv.ParseFloat(v, 64)
			case float64:
				px = v
			}
			switch v := r[1].(type) {
			case string:
				sz, _ = strconv.ParseFloat(v, 64)
			case float64:
				sz = v
			}
			if sz > 0 {
				out = append(out, ws.Level{px, sz})
			}
		}
		return out
	}
	return &ws.Snapshot{
		Symbol: token,
		Bids:   parseSide(msg.Data.Bids),
		Asks:   parseSide(msg.Data.Asks),
	}, nil
}

func (a *Futures) Heartbeat() []byte {
	frame, _ := ws.MarshalJSON(map[string]any{"id": time.Now().UnixNano(), "type": "ping"})
	return frame
}
func (a *Futures) HeartbeatInterval() time.Duration { return 15 * time.Second }
func (a *Futures) PongFor(_ []byte) []byte          { return nil }
func (a *Futures) UseLibPings() bool                { return false }
func (a *Futures) SubscribeDelay() time.Duration    { return 400 * time.Millisecond }
func (a *Futures) MaxSymbols() int                  { return 100 }
func (a *Futures) DecompressGzip() bool             { return false }

func (a *Futures) OnReconnect() {
	a.books = make(map[string]*book)
}
