// Package aster — funding adapter for Aster (Binance fork).
//
// Same protocol as Binance but on aster hosts. WS path/REST endpoint
// differ; everything else identical.
package aster

import (
	"context"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

const (
	wsURL   = "wss://fstream.asterdex.com/ws/!markPrice@arr@1s"
	restURL = "https://fapi.asterdex.com/fapi/v1/premiumIndex"
)

type Adapter struct{}

func New() *Adapter { return &Adapter{} }

func (a *Adapter) Name() string                          { return "aster" }
func (a *Adapter) URL(_ context.Context) (string, error) { return wsURL, nil }
func (a *Adapter) BuildSubscribe(_ []string) [][]byte    { return nil }

func (a *Adapter) ParseWS(frame []byte) ([]funding.Tick, error) {
	var rows []struct {
		Symbol      string `json:"s"`
		MarkPrice   string `json:"p"`
		IndexPrice  string `json:"i"`
		Rate        string `json:"r"`
		NextFunding int64  `json:"T"`
	}
	if err := ws.UnmarshalJSON(frame, &rows); err != nil {
		return nil, nil
	}
	out := make([]funding.Tick, 0, len(rows))
	for _, r := range rows {
		if !strings.HasSuffix(r.Symbol, "USDT") {
			continue
		}
		token := strings.TrimSuffix(r.Symbol, "USDT")
		mark, _ := strconv.ParseFloat(r.MarkPrice, 64)
		idx, _ := strconv.ParseFloat(r.IndexPrice, 64)
		rate, _ := strconv.ParseFloat(r.Rate, 64)
		out = append(out, funding.Tick{
			Symbol:      token,
			Rate:        rate,
			MarkPrice:   mark,
			IndexPrice:  idx,
			NextFunding: time.UnixMilli(r.NextFunding),
			IntervalH:   8,
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
		Symbol        string `json:"symbol"`
		MarkPrice     string `json:"markPrice"`
		IndexPrice    string `json:"indexPrice"`
		LastFundRate  string `json:"lastFundingRate"`
		NextFundingTs int64  `json:"nextFundingTime"`
	}
	if err := funding.HTTPGet(ctx, restURL, &rows); err != nil {
		return nil, err
	}
	out := make([]funding.Tick, 0, len(rows))
	for _, r := range rows {
		if !strings.HasSuffix(r.Symbol, "USDT") {
			continue
		}
		token := strings.TrimSuffix(r.Symbol, "USDT")
		mark, _ := strconv.ParseFloat(r.MarkPrice, 64)
		idx, _ := strconv.ParseFloat(r.IndexPrice, 64)
		rate, _ := strconv.ParseFloat(r.LastFundRate, 64)
		out = append(out, funding.Tick{
			Symbol:      token,
			Rate:        rate,
			MarkPrice:   mark,
			IndexPrice:  idx,
			NextFunding: time.UnixMilli(r.NextFundingTs),
			IntervalH:   8,
		})
	}
	return out, nil
}

func (a *Adapter) BackstopInterval() time.Duration { return 2 * time.Second }
