// Package mexc — MEXC contract (USDT-margined linear perp).
//
// URL: wss://contract.mexc.com/edge
// Subscribe: {"method":"sub.depth.full","param":{"symbol":"BTC_USDT","limit":20}}
//
// Inbound (full snapshot every push, NOT diff):
//   {"channel":"push.depth.full","data":{"asks":[[px,sz,n],...],"bids":[...]},
//    "symbol":"BTC_USDT","ts":...}
//
// Heartbeat: {"method":"ping"} → server replies {"channel":"pong"}.
package mexc

import (
	"context"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const futuresWS = "wss://contract.mexc.com/edge"

type Futures struct {
	store *cache.Store
}

func NewFutures(store *cache.Store) *ws.Runner {
	a := &Futures{store: store}
	return ws.NewRunner(a, func(_ string, snap ws.Snapshot) {
		store.Store("mexc", snap.Symbol, snap, "ws")
	})
}

func (a *Futures) Name() string                          { return "mexc" }
func (a *Futures) URL(_ context.Context) (string, error) { return futuresWS, nil }

func (a *Futures) BuildSubscribe(symbols []string) [][]byte {
	frames := make([][]byte, 0, len(symbols))
	for _, s := range symbols {
		f := map[string]any{
			"method": "sub.depth.full",
			"param":  map[string]any{"symbol": strings.ToUpper(s) + "_USDT", "limit": 20},
		}
		b, _ := ws.MarshalJSON(f)
		frames = append(frames, b)
	}
	return frames
}

func (a *Futures) Parse(frame []byte) (*ws.Snapshot, error) {
	var msg struct {
		Channel string `json:"channel"`
		Symbol  string `json:"symbol"`
		Data    struct {
			Bids [][]float64 `json:"bids"`
			Asks [][]float64 `json:"asks"`
		} `json:"data"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Channel != "push.depth.full" {
		return nil, nil
	}
	if !strings.HasSuffix(msg.Symbol, "_USDT") {
		return nil, nil
	}
	token := strings.TrimSuffix(msg.Symbol, "_USDT")

	snap := &ws.Snapshot{Symbol: token}
	for _, r := range msg.Data.Bids {
		if len(r) < 2 || r[1] <= 0 {
			continue
		}
		snap.Bids = append(snap.Bids, ws.Level{r[0], r[1]})
	}
	for _, r := range msg.Data.Asks {
		if len(r) < 2 || r[1] <= 0 {
			continue
		}
		snap.Asks = append(snap.Asks, ws.Level{r[0], r[1]})
	}
	return snap, nil
}

// MEXC contract requires {"method":"ping"} every ~20s.
func (a *Futures) Heartbeat() []byte                { return []byte(`{"method":"ping"}`) }
func (a *Futures) HeartbeatInterval() time.Duration { return 18 * time.Second }
func (a *Futures) PongFor(_ []byte) []byte          { return nil }
func (a *Futures) UseLibPings() bool                { return false }
func (a *Futures) SubscribeDelay() time.Duration    { return 0 }
func (a *Futures) MaxSymbols() int                  { return 0 }
func (a *Futures) DecompressGzip() bool             { return false }
func (a *Futures) OnReconnect()                     {}
