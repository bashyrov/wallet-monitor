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
	wsURL        = "wss://fx-ws.gateio.ws/v4/ws/usdt"
	contractsURL = "https://api.gateio.ws/api/v4/futures/usdt/contracts"
	tickersURL   = "https://api.gateio.ws/api/v4/futures/usdt/tickers"
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
			Contract        string `json:"contract"`
			Last            string `json:"last"`
			MarkPrice       string `json:"mark_price"`
			IndexPrice      string `json:"index_price"`
			FundingRate     string `json:"funding_rate"`
			Volume24hUSD    string `json:"volume_24h_usd"`    // primary 24h volume in USDT
			Volume24hQuote  string `json:"volume_24h_quote"`  // fallback when *_usd absent
			Volume24hSettle string `json:"volume_24h_settle"` // legacy fallback
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
		// Gate ships 24h volume under one of three field names depending
		// on the contract / API version. Prefer `volume_24h_usd` (matches
		// Python adapter) and fall back the chain.
		vol := parseFloat(r.Volume24hUSD)
		if vol == 0 {
			vol = parseFloat(r.Volume24hQuote)
		}
		if vol == 0 {
			vol = parseFloat(r.Volume24hSettle)
		}
		out = append(out, funding.Tick{
			Symbol:     token,
			Rate:       rate,
			MarkPrice:  mark,
			IndexPrice: idx,
			Volume24h:  vol,
			// IntervalH NOT set here — Gate's WS payload doesn't carry the
			// funding interval, and forcing 8 wipes the real per-pair
			// value the REST backstop fetches from /contracts. The store's
			// merge preserves the last non-zero IntervalH automatically.
		})
	}
	return out, nil
}

func parseFloat(s string) float64 {
	v, _ := strconv.ParseFloat(s, 64)
	return v
}

func (a *Adapter) Heartbeat() []byte                { return nil }
func (a *Adapter) HeartbeatInterval() time.Duration { return 0 }
func (a *Adapter) PongFor(_ []byte) []byte          { return nil }
func (a *Adapter) UseLibPings() bool                { return true }
func (a *Adapter) DecompressGzip() bool             { return false }

func (a *Adapter) BackstopFetch(ctx context.Context, _ []string) ([]funding.Tick, error) {
	// Two parallel calls:
	//   /contracts — funding_rate, funding_interval, mark_price, next_apply
	//   /tickers   — volume_24h_settle / _quote / _usd
	// Gate doesn't put 24h volume on /contracts and doesn't put
	// funding_interval on /tickers, so we need both. They're cheap
	// (cached upstream) and bounded; a 2× round-trip on a 2 s tick is
	// negligible against the freshness gain.
	var rows []struct {
		Name             string `json:"name"`
		LastPrice        string `json:"last_price"`
		MarkPrice        string `json:"mark_price"`
		IndexPrice       string `json:"index_price"`
		FundingRate      string `json:"funding_rate"`
		FundingNextApply int64  `json:"funding_next_apply"`
		FundingInterval  int64  `json:"funding_interval"`
	}
	if err := funding.HTTPGet(ctx, contractsURL, &rows); err != nil {
		return nil, err
	}

	// Best-effort volume map — non-fatal on failure.
	volBySymbol := make(map[string]float64, len(rows))
	var tickers []struct {
		Contract        string `json:"contract"`
		Volume24hSettle string `json:"volume_24h_settle"`
		Volume24hUSD    string `json:"volume_24h_usd"`
		Volume24hQuote  string `json:"volume_24h_quote"`
	}
	if err := funding.HTTPGet(ctx, tickersURL, &tickers); err == nil {
		for _, tk := range tickers {
			if !strings.HasSuffix(tk.Contract, "_USDT") {
				continue
			}
			token := strings.TrimSuffix(tk.Contract, "_USDT")
			v := parseFloat(tk.Volume24hUSD)
			if v == 0 {
				v = parseFloat(tk.Volume24hQuote)
			}
			if v == 0 {
				v = parseFloat(tk.Volume24hSettle)
			}
			if v > 0 {
				volBySymbol[token] = v
			}
		}
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
			Symbol:     token,
			Rate:       rate,
			MarkPrice:  mark,
			IndexPrice: idx,
			Volume24h:  volBySymbol[token],
			IntervalH:  intH,
		}
		if r.FundingNextApply > 0 {
			t.NextFunding = time.Unix(r.FundingNextApply, 0)
		}
		out = append(out, t)
	}
	return out, nil
}

func (a *Adapter) BackstopInterval() time.Duration { return 2 * time.Second }
