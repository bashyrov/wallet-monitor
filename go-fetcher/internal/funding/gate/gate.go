// Package gate — funding adapter for Gate.io USDT-perp.
//
// WS:   wss://fx-ws.gateio.ws/v4/ws/usdt
//       Subscribe channel "futures.tickers", payload [contract, ...]
// REST: https://api.gateio.ws/api/v4/futures/usdt/contracts — contract
//       metadata including funding_rate, mark_price, last_price, vol_24h.
package gate

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const (
	wsURL   = "wss://fx-ws.gateio.ws/v4/ws/usdt"
	restURL = "https://api.gateio.ws/api/v4/futures/usdt/contracts"
)

type Adapter struct{}

func New() *Adapter { return &Adapter{} }

func (a *Adapter) Name() string                          { return "gate" }
func (a *Adapter) URL(_ context.Context) (string, error) { return wsURL, nil }

func (a *Adapter) BuildSubscribe(symbols []string) [][]byte {
	contracts := make([]string, len(symbols))
	for i, s := range symbols {
		contracts[i] = strings.ToUpper(s) + "_USDT"
	}
	frame := map[string]any{
		"time":    time.Now().Unix(),
		"channel": "futures.tickers",
		"event":   "subscribe",
		"payload": contracts,
	}
	b, _ := ws.MarshalJSON(frame)
	return [][]byte{b}
}

func (a *Adapter) ParseWS(frame []byte) ([]funding.Tick, error) {
	var msg struct {
		Channel string `json:"channel"`
		Event   string `json:"event"`
		Result  []struct {
			Contract       string  `json:"contract"`
			Last           string  `json:"last"`
			MarkPrice      string  `json:"mark_price"`
			IndexPrice     string  `json:"index_price"`
			FundingRate    string  `json:"funding_rate"`
			Volume24hUSD   string  `json:"volume_24h_settle"`
		} `json:"result"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Channel != "futures.tickers" || msg.Event != "update" {
		return nil, nil
	}
	out := make([]funding.Tick, 0, len(msg.Result))
	for _, r := range msg.Result {
		if !strings.HasSuffix(r.Contract, "_USDT") {
			continue
		}
		token := strings.TrimSuffix(r.Contract, "_USDT")
		mark, _ := strconv.ParseFloat(r.MarkPrice, 64)
		idx, _ := strconv.ParseFloat(r.IndexPrice, 64)
		rate, _ := strconv.ParseFloat(r.FundingRate, 64)
		vol, _ := strconv.ParseFloat(r.Volume24hUSD, 64)
		out = append(out, funding.Tick{
			Symbol:    token,
			Rate:      rate,
			MarkPrice: mark,
			IndexPrice: idx,
			Volume24h: vol,
			IntervalH: 8,
		})
	}
	return out, nil
}

func (a *Adapter) Heartbeat() []byte                { return nil }
func (a *Adapter) HeartbeatInterval() time.Duration { return 0 }
func (a *Adapter) PongFor(_ []byte) []byte          { return nil }
func (a *Adapter) UseLibPings() bool                { return true }
func (a *Adapter) DecompressGzip() bool             { return false }

func (a *Adapter) BackstopFetch(ctx context.Context, _ []string) ([]funding.Tick, error) {
	var rows []struct {
		Name             string `json:"name"`
		LastPrice        string `json:"last_price"`
		MarkPrice        string `json:"mark_price"`
		IndexPrice       string `json:"index_price"`
		FundingRate      string `json:"funding_rate"`
		FundingNextApply int64  `json:"funding_next_apply"`
		FundingInterval  int64  `json:"funding_interval"`
	}
	if err := funding.HTTPGet(ctx, restURL, &rows); err != nil {
		return nil, err
	}
	out := make([]funding.Tick, 0, len(rows))
	for _, r := range rows {
		if !strings.HasSuffix(r.Name, "_USDT") {
			continue
		}
		token := strings.TrimSuffix(r.Name, "_USDT")
		mark, _ := strconv.ParseFloat(r.MarkPrice, 64)
		idx, _ := strconv.ParseFloat(r.IndexPrice, 64)
		rate, _ := strconv.ParseFloat(r.FundingRate, 64)
		intH := float64(r.FundingInterval) / 3600
		if intH <= 0 {
			intH = 8
		}
		t := funding.Tick{
			Symbol:    token,
			Rate:      rate,
			MarkPrice: mark,
			IndexPrice: idx,
			IntervalH: intH,
		}
		if r.FundingNextApply > 0 {
			t.NextFunding = time.Unix(r.FundingNextApply, 0)
		}
		out = append(out, t)
	}
	return out, nil
}

func (a *Adapter) BackstopInterval() time.Duration { return 2 * time.Second }
