package aster

import (
	"testing"
)

// Aster uses Binance's combined-stream protocol exactly: markPrice@arr@1s
// for funding+mark; ticker@arr for volume.
func TestParseWS_MarkPriceStream(t *testing.T) {
	a := New()
	frame := []byte(`{"stream":"!markPrice@arr@1s","data":[
		{"s":"BTCUSDT","p":"60000.5","i":"60000","r":"0.0001","T":1718000028000}
	]}`)
	ticks, err := a.ParseWS(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(ticks) != 1 {
		t.Fatalf("len: %d", len(ticks))
	}
	tk := ticks[0]
	if tk.Symbol != "BTC" || tk.MarkPrice != 60000.5 || tk.Rate != 0.0001 {
		t.Errorf("BTC: %+v", tk)
	}
	if tk.NextFunding.UnixMilli() != 1718000028000 {
		t.Errorf("next funding: %v", tk.NextFunding)
	}
}

func TestParseWS_TickerStreamVolumeOnly(t *testing.T) {
	a := New()
	frame := []byte(`{"stream":"!ticker@arr","data":[
		{"s":"BTCUSDT","q":"1000000000"},
		{"s":"ETHUSDT","q":"500000000"}
	]}`)
	ticks, _ := a.ParseWS(frame)
	if len(ticks) != 2 {
		t.Fatalf("len: %d", len(ticks))
	}
	if ticks[0].Volume24h != 1e9 || ticks[1].Volume24h != 5e8 {
		t.Errorf("volumes: %v %v", ticks[0].Volume24h, ticks[1].Volume24h)
	}
}

func TestParseWS_NonUSDTSymbolFiltered(t *testing.T) {
	a := New()
	frame := []byte(`{"stream":"!markPrice@arr@1s","data":[
		{"s":"BTCBUSD","p":"60000","i":"60000","r":"0.0001","T":1}
	]}`)
	ticks, _ := a.ParseWS(frame)
	if len(ticks) != 0 {
		t.Errorf("non-USDT should be filtered, got %v", ticks)
	}
}

func TestParseWS_UnknownStreamIgnored(t *testing.T) {
	a := New()
	ticks, _ := a.ParseWS([]byte(`{"stream":"btcusdt@kline_1m","data":{}}`))
	if ticks != nil {
		t.Errorf("unknown stream should produce nil, got %v", ticks)
	}
}

func TestParseWS_NegativeRate(t *testing.T) {
	a := New()
	ticks, _ := a.ParseWS([]byte(`{"stream":"!markPrice@arr@1s","data":[
		{"s":"ETHUSDT","p":"3000","i":"3000","r":"-0.0002","T":1}
	]}`))
	if len(ticks) != 1 || ticks[0].Rate != -0.0002 {
		t.Errorf("negative rate: %v", ticks)
	}
}
