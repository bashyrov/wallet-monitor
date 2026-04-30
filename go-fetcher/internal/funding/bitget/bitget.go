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
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const (
	wsURL   = "wss://ws.bitget.com/v2/ws/public"
	restURL = "https://api.bitget.com/api/v2/mix/market/tickers?productType=USDT-FUTURES"
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
			NextFundingTime string `json:"nextFundingTime"`
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
			Symbol:    token,
			Rate:      rate,
			MarkPrice: mark,
			IndexPrice: idx,
			Volume24h: vol,
			IntervalH: 8,
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

func (a *Adapter) BackstopFetch(ctx context.Context, _ []string) ([]funding.Tick, error) {
	var doc struct {
		Data []struct {
			Symbol          string `json:"symbol"`
			LastPr          string `json:"lastPr"`
			IndexPrice      string `json:"indexPrice"`
			MarkPrice       string `json:"markPrice"`
			FundingRate     string `json:"fundingRate"`
			NextFundingTime string `json:"nextFundingTime"`
			QuoteVolume     string `json:"quoteVolume"`
		} `json:"data"`
	}
	if err := funding.HTTPGet(ctx, restURL, &doc); err != nil {
		return nil, err
	}
	out := make([]funding.Tick, 0, len(doc.Data))
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
		nextMs, _ := strconv.ParseInt(r.NextFundingTime, 10, 64)
		t := funding.Tick{
			Symbol:    token,
			Rate:      rate,
			MarkPrice: mark,
			IndexPrice: idx,
			Volume24h: vol,
			IntervalH: 8,
		}
		if nextMs > 0 {
			t.NextFunding = time.UnixMilli(nextMs)
		}
		out = append(out, t)
	}
	return out, nil
}

func (a *Adapter) BackstopInterval() time.Duration { return 2 * time.Second }
