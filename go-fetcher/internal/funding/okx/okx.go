// Package okx — funding adapter for OKX SWAP (USDT-perp).
//
// OKX has TWO relevant WS channels — funding-rate (just rate) and tickers
// (mark price + volume). To keep the adapter simple, we subscribe both
// channels on the same connection.
//
// WS:   wss://ws.okx.com:8443/ws/v5/public
//       channel "funding-rate" + "tickers"
// REST: https://www.okx.com/api/v5/market/tickers?instType=SWAP
//       https://www.okx.com/api/v5/public/funding-rate?instId=...  (per-symbol; expensive)
//
// Backstop strategy: only the tickers REST sweep on every cycle (cheap,
// returns all in one call). Funding rate is supplied by the WS funding-rate
// channel; if WS is dead, screener basis still works from mark+spot.
package okx

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const (
	wsURL          = "wss://ws.okx.com:8443/ws/v5/public"
	restTickersURL = "https://www.okx.com/api/v5/market/tickers?instType=SWAP"
)

type Adapter struct{}

func New() *Adapter { return &Adapter{} }

func (a *Adapter) Name() string                          { return "okx" }
func (a *Adapter) URL(_ context.Context) (string, error) { return wsURL, nil }

func (a *Adapter) BuildSubscribe(symbols []string) [][]byte {
	args := make([]map[string]string, 0, len(symbols)*2)
	for _, s := range symbols {
		inst := strings.ToUpper(s) + "-USDT-SWAP"
		args = append(args,
			map[string]string{"channel": "funding-rate", "instId": inst},
			map[string]string{"channel": "tickers", "instId": inst},
		)
	}
	frame := map[string]any{"op": "subscribe", "args": args}
	b, _ := ws.MarshalJSON(frame)
	return [][]byte{b}
}

func (a *Adapter) ParseWS(frame []byte) ([]funding.Tick, error) {
	var msg struct {
		Event string `json:"event"`
		Arg   struct {
			Channel string `json:"channel"`
			InstID  string `json:"instId"`
		} `json:"arg"`
		Data []map[string]any `json:"data"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Event != "" {
		return nil, nil
	}
	if !strings.HasSuffix(msg.Arg.InstID, "-USDT-SWAP") {
		return nil, nil
	}
	token := strings.TrimSuffix(msg.Arg.InstID, "-USDT-SWAP")
	out := make([]funding.Tick, 0, len(msg.Data))
	for _, d := range msg.Data {
		t := funding.Tick{Symbol: token, IntervalH: 8}
		switch msg.Arg.Channel {
		case "funding-rate":
			if v, ok := d["fundingRate"].(string); ok {
				t.Rate, _ = strconv.ParseFloat(v, 64)
			}
			if v, ok := d["nextFundingTime"].(string); ok {
				ms, _ := strconv.ParseInt(v, 10, 64)
				if ms > 0 {
					t.NextFunding = time.UnixMilli(ms)
				}
			}
		case "tickers":
			if v, ok := d["last"].(string); ok {
				t.MarkPrice, _ = strconv.ParseFloat(v, 64)
			}
			if v, ok := d["idxPx"].(string); ok {
				t.IndexPrice, _ = strconv.ParseFloat(v, 64)
			}
			if v, ok := d["volCcy24h"].(string); ok {
				t.Volume24h, _ = strconv.ParseFloat(v, 64)
			}
		default:
			continue
		}
		out = append(out, t)
	}
	return out, nil
}

// OKX needs app-level "ping"/"pong" — same as orderbook adapter.
func (a *Adapter) Heartbeat() []byte                { return []byte("ping") }
func (a *Adapter) HeartbeatInterval() time.Duration { return 25 * time.Second }
func (a *Adapter) PongFor(_ []byte) []byte          { return nil }
func (a *Adapter) UseLibPings() bool                { return false }
func (a *Adapter) DecompressGzip() bool             { return false }

func (a *Adapter) BackstopFetch(ctx context.Context, _ []string) ([]funding.Tick, error) {
	var doc struct {
		Data []struct {
			InstID    string `json:"instId"`
			Last      string `json:"last"`
			IdxPx     string `json:"idxPx"`
			VolCcy24h string `json:"volCcy24h"`
		} `json:"data"`
	}
	if err := funding.HTTPGet(ctx, restTickersURL, &doc); err != nil {
		return nil, err
	}
	out := make([]funding.Tick, 0, len(doc.Data))
	for _, r := range doc.Data {
		if !strings.HasSuffix(r.InstID, "-USDT-SWAP") {
			continue
		}
		token := strings.TrimSuffix(r.InstID, "-USDT-SWAP")
		mark, _ := strconv.ParseFloat(r.Last, 64)
		idx, _ := strconv.ParseFloat(r.IdxPx, 64)
		vol, _ := strconv.ParseFloat(r.VolCcy24h, 64)
		out = append(out, funding.Tick{
			Symbol:     token,
			MarkPrice:  mark,
			IndexPrice: idx,
			Volume24h:  vol,
			IntervalH:  8,
		})
	}
	return out, nil
}

func (a *Adapter) BackstopInterval() time.Duration { return 2 * time.Second }
