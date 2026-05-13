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
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const wsURL = "wss://ws.okx.com:8443/ws/v5/public"

// URLs — vars (not const) so package tests can override.
var (
	restTickersURL     = "https://www.okx.com/api/v5/market/tickers?instType=SWAP"
	restFundingRateURL = "https://www.okx.com/api/v5/public/funding-rate?instId="
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
				// volCcy24h is in BASE units (see BackstopFetch comment).
				// Convert to USD via mark price.
				volBase, _ := strconv.ParseFloat(v, 64)
				t.Volume24h = volBase * t.MarkPrice
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

func (a *Adapter) BackstopFetch(ctx context.Context, symbols []string) ([]funding.Tick, error) {
	// Bulk tickers — mark price, index, 24h volume (one call, all symbols).
	var tickerDoc struct {
		Data []struct {
			InstID    string `json:"instId"`
			Last      string `json:"last"`
			IdxPx     string `json:"idxPx"`
			VolCcy24h string `json:"volCcy24h"`
		} `json:"data"`
	}
	if err := funding.HTTPGet(ctx, restTickersURL, &tickerDoc); err != nil {
		return nil, err
	}
	byToken := make(map[string]*funding.Tick, len(tickerDoc.Data))
	for _, r := range tickerDoc.Data {
		if !strings.HasSuffix(r.InstID, "-USDT-SWAP") {
			continue
		}
		token := strings.TrimSuffix(r.InstID, "-USDT-SWAP")
		mark, _ := strconv.ParseFloat(r.Last, 64)
		idx, _ := strconv.ParseFloat(r.IdxPx, 64)
		// OKX `volCcy24h` for SWAP is volume in BASE currency units
		// (LITE coins for LITE-USDT-SWAP, BTC for BTC-USDT-SWAP), NOT
		// in USDT. Convert to USD via mark price so downstream filters
		// (and the screener column "Vol") see USD numbers like every
		// other venue. Without this LITE on OKX read as $3.5K instead
		// of the real $3.5M and got dropped by any volume floor.
		volBase, _ := strconv.ParseFloat(r.VolCcy24h, 64)
		vol := volBase * mark
		byToken[token] = &funding.Tick{
			Symbol:     token,
			MarkPrice:  mark,
			IndexPrice: idx,
			Volume24h:  vol,
			IntervalH:  8,
		}
	}

	// Per-symbol funding-rate fetch for subscribed symbols.
	// OKX's WS funding-rate channel is event-driven — it only pushes when
	// the rate changes (near settlement), so it cannot populate the initial
	// rate on startup. REST fill is the only reliable source.
	if len(symbols) > 0 {
		type rateEntry struct {
			token       string
			rate        float64
			nextFunding time.Time
			intervalH   float64
		}
		entries := make(chan rateEntry, len(symbols))
		sem := make(chan struct{}, 8) // max 8 parallel HTTP calls
		var wg sync.WaitGroup
		for _, sym := range symbols {
			sym := sym
			wg.Add(1)
			go func() {
				defer wg.Done()
				sem <- struct{}{}
				defer func() { <-sem }()
				inst := strings.ToUpper(sym) + "-USDT-SWAP"
				var doc struct {
					Data []struct {
						FundingRate     string `json:"fundingRate"`
						NextFundingTime string `json:"nextFundingTime"`
						FundingTime     string `json:"fundingTime"`
					} `json:"data"`
				}
				if err := funding.HTTPGet(ctx, restFundingRateURL+inst, &doc); err != nil || len(doc.Data) == 0 {
					return
				}
				d := doc.Data[0]
				rate, _ := strconv.ParseFloat(d.FundingRate, 64)
				nextMs, _ := strconv.ParseInt(d.NextFundingTime, 10, 64)
				var nf time.Time
				if nextMs > 0 {
					nf = time.UnixMilli(nextMs)
				}
				// Derive interval from fundingTime → nextFundingTime.
				ivl := 8.0
				fundMs, _ := strconv.ParseInt(d.FundingTime, 10, 64)
				if fundMs > 0 && nextMs > fundMs {
					if h := float64(nextMs-fundMs) / 3_600_000; h >= 1 {
						ivl = h
					}
				}
				entries <- rateEntry{token: strings.ToUpper(sym), rate: rate, nextFunding: nf, intervalH: ivl}
			}()
		}
		go func() { wg.Wait(); close(entries) }()
		for e := range entries {
			if t := byToken[e.token]; t != nil {
				t.Rate = e.rate
				t.NextFunding = e.nextFunding
				t.IntervalH = e.intervalH
			}
		}
	}

	out := make([]funding.Tick, 0, len(byToken))
	for _, t := range byToken {
		out = append(out, *t)
	}
	return out, nil
}

// 60s backstop: WS tickers channel streams mark/vol in real-time;
// WS funding-rate only fires at settlement. REST fills rate on startup
// and catches any WS gaps. Rate changes at most 3x/day so 60s is plenty.
func (a *Adapter) BackstopInterval() time.Duration { return 60 * time.Second }
