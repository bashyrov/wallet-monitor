// Package bitget — funding adapter for Bitget V2 USDT-FUTURES.
//
// WS:   wss://ws.bitget.com/v2/ws/public
//       channel "ticker", instType "USDT-FUTURES" — push includes
//       fundingRate, lastPr, baseVolume, quoteVolume, nextFundingTime.
// REST: https://api.bitget.com/api/v2/mix/market/tickers?productType=USDT-FUTURES
//
// Same lib-ping + heartbeat fixes as the orderbook adapter (bug #4 + #6
// from PLAN — the Bitget V2 server CLOSES the connection if we don't
// send a literal text "ping" every <30s, AND ignores lib-level WS
// pings). Re-deriving here keeps the funding adapter self-contained.
package bitget

import (
	"context"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const (
	wsURL          = "wss://ws.bitget.com/v2/ws/public"
	restURL        = "https://api.bitget.com/api/v2/mix/market/tickers?productType=USDT-FUTURES"
	restFundRateURL = "https://api.bitget.com/api/v2/mix/market/current-fund-rate?productType=USDT-FUTURES&symbol="
)

type Adapter struct{}

func New() *Adapter { return &Adapter{} }

func (a *Adapter) Name() string                          { return "bitget" }
func (a *Adapter) URL(_ context.Context) (string, error) { return wsURL, nil }

func (a *Adapter) BuildSubscribe(symbols []string) [][]byte {
	args := make([]map[string]string, len(symbols))
	for i, s := range symbols {
		args[i] = map[string]string{
			"instType": "USDT-FUTURES",
			"channel":  "ticker",
			"instId":   strings.ToUpper(s) + "USDT",
		}
	}
	frame := map[string]any{"op": "subscribe", "args": args}
	b, _ := ws.MarshalJSON(frame)
	return [][]byte{b}
}

func (a *Adapter) ParseWS(frame []byte) ([]funding.Tick, error) {
	var msg struct {
		Event string `json:"event"`
		Arg   struct {
			InstType string `json:"instType"`
			Channel  string `json:"channel"`
			InstID   string `json:"instId"`
		} `json:"arg"`
		Data []struct {
			InstID          string `json:"instId"`
			LastPr          string `json:"lastPr"`
			IndexPrice      string `json:"indexPrice"`
			MarkPrice       string `json:"markPrice"`
			FundingRate     string `json:"fundingRate"`
			NextFundingTime string `json:"nextFundingTime"` // string ms; absent from update frames
			QuoteVolume     string `json:"quoteVolume"`
			BaseVolume      string `json:"baseVolume"`
		} `json:"data"`
	}
	if err := ws.UnmarshalJSON(frame, &msg); err != nil {
		return nil, err
	}
	if msg.Event != "" || msg.Arg.Channel != "ticker" {
		return nil, nil
	}
	out := make([]funding.Tick, 0, len(msg.Data))
	for _, d := range msg.Data {
		if !strings.HasSuffix(d.InstID, "USDT") {
			continue
		}
		token := strings.TrimSuffix(d.InstID, "USDT")
		rate, _ := strconv.ParseFloat(d.FundingRate, 64)
		mark, _ := strconv.ParseFloat(d.MarkPrice, 64)
		if mark == 0 {
			mark, _ = strconv.ParseFloat(d.LastPr, 64)
		}
		idx, _ := strconv.ParseFloat(d.IndexPrice, 64)
		vol, _ := strconv.ParseFloat(d.QuoteVolume, 64)
		nextMs, _ := strconv.ParseInt(d.NextFundingTime, 10, 64)
		t := funding.Tick{
			Symbol:     token,
			Rate:       rate,
			MarkPrice:  mark,
			IndexPrice: idx,
			Volume24h:  vol,
			// IntervalH NOT set — Bitget's WS payload doesn't carry the
			// per-pair interval; forcing 8 wipes the real value (some
			// pairs are 4h). The store preserves the last non-zero
			// value, so once the REST backstop sets it the WS stops
			// stomping it back to default.
		}
		if nextMs > 0 {
			t.NextFunding = time.UnixMilli(nextMs)
		}
		out = append(out, t)
	}
	return out, nil
}

// Bitget V2 quirks (bug #4 + #6) — mirror orderbook adapter.
func (a *Adapter) Heartbeat() []byte                { return []byte("ping") }
func (a *Adapter) HeartbeatInterval() time.Duration { return 25 * time.Second }
func (a *Adapter) PongFor(_ []byte) []byte          { return nil }
func (a *Adapter) UseLibPings() bool                { return false }
func (a *Adapter) DecompressGzip() bool             { return false }

func (a *Adapter) BackstopFetch(ctx context.Context, symbols []string) ([]funding.Tick, error) {
	// Bulk tickers — rate, mark, vol. nextFundingTime is NOT in this response.
	var doc struct {
		Data []struct {
			Symbol      string `json:"symbol"`
			LastPr      string `json:"lastPr"`
			IndexPrice  string `json:"indexPrice"`
			MarkPrice   string `json:"markPrice"`
			FundingRate string `json:"fundingRate"`
			QuoteVolume string `json:"quoteVolume"`
		} `json:"data"`
	}
	if err := funding.HTTPGet(ctx, restURL, &doc); err != nil {
		return nil, err
	}
	byToken := make(map[string]*funding.Tick, len(doc.Data))
	for _, r := range doc.Data {
		if !strings.HasSuffix(r.Symbol, "USDT") {
			continue
		}
		token := strings.TrimSuffix(r.Symbol, "USDT")
		rate, _ := strconv.ParseFloat(r.FundingRate, 64)
		mark, _ := strconv.ParseFloat(r.MarkPrice, 64)
		if mark == 0 {
			mark, _ = strconv.ParseFloat(r.LastPr, 64)
		}
		idx, _ := strconv.ParseFloat(r.IndexPrice, 64)
		vol, _ := strconv.ParseFloat(r.QuoteVolume, 64)
		byToken[token] = &funding.Tick{
			Symbol:     token,
			Rate:       rate,
			MarkPrice:  mark,
			IndexPrice: idx,
			Volume24h:  vol,
			IntervalH:  8,
		}
	}

	// Per-symbol current-fund-rate — nextFunding + real intervalH.
	// Bulk tickers omit nextFundingTime; current-fund-rate provides "nextUpdate".
	if len(symbols) > 0 {
		type rateEntry struct {
			token       string
			nextFunding time.Time
			intervalH   float64
		}
		// Fallback for symbols the per-symbol sweep doesn't cover within
		// the budget — populate with the next 8h UTC boundary (most
		// bitget symbols pay 8h; a small subset pay 4h but those land
		// on the same wall-clock cycle anyway). Without this, the long
		// tail of bitget rows shipped with next_ts=0 (77/488 in prod).
		const cyclehrs = 8
		nextFallback := time.Now().UTC().Truncate(time.Duration(cyclehrs) * time.Hour).Add(time.Duration(cyclehrs) * time.Hour)
		for token, t := range byToken {
			if t.NextFunding.IsZero() {
				t.NextFunding = nextFallback
				byToken[token] = t
			}
		}

		entries := make(chan rateEntry, len(symbols))
		sem := make(chan struct{}, 8)
		var wg sync.WaitGroup
		for _, sym := range symbols {
			sym := sym
			wg.Add(1)
			go func() {
				defer wg.Done()
				sem <- struct{}{}
				defer func() { <-sem }()
				symUp := strings.ToUpper(sym) + "USDT"
				var doc struct {
					Data []struct {
						FundingRateInterval string `json:"fundingRateInterval"` // hours as string
						NextUpdate          string `json:"nextUpdate"`          // ms as string
					} `json:"data"`
				}
				if err := funding.HTTPGet(ctx, restFundRateURL+symUp, &doc); err != nil || len(doc.Data) == 0 {
					return
				}
				d := doc.Data[0]
				nextMs, _ := strconv.ParseInt(d.NextUpdate, 10, 64)
				var nf time.Time
				if nextMs > 0 {
					nf = time.UnixMilli(nextMs)
				}
				ivl, _ := strconv.ParseFloat(d.FundingRateInterval, 64)
				if ivl <= 0 {
					ivl = 8
				}
				entries <- rateEntry{token: strings.ToUpper(sym), nextFunding: nf, intervalH: ivl}
			}()
		}
		go func() { wg.Wait(); close(entries) }()
		for e := range entries {
			if t := byToken[e.token]; t != nil {
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

func (a *Adapter) BackstopInterval() time.Duration { return 2 * time.Second }
