package binance

import (
	"testing"
)

func TestParseWS_MarkPriceStream(t *testing.T) {
	a := New()
	// Combined-stream wrapper around !markPrice@arr@1s
	frame := []byte(`{"stream":"!markPrice@arr@1s","data":[
		{"s":"BTCUSDT","p":"60000.5","i":"60000.0","r":"0.0001","T":1718000028000},
		{"s":"ETHUSDT","p":"3000.0","i":"2999.5","r":"-0.0002","T":1718000028000}
	]}`)
	ticks, err := a.ParseWS(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(ticks) != 2 {
		t.Fatalf("ticks: want 2 got %d", len(ticks))
	}
	if ticks[0].Symbol != "BTC" || ticks[0].MarkPrice != 60000.5 {
		t.Errorf("BTC: %+v", ticks[0])
	}
	if ticks[0].Rate != 0.0001 {
		t.Errorf("BTC rate: %v", ticks[0].Rate)
	}
	if ticks[1].Symbol != "ETH" || ticks[1].Rate != -0.0002 {
		t.Errorf("ETH (negative rate): %+v", ticks[1])
	}
	for _, tk := range ticks {
		if tk.IntervalH != 8 {
			t.Errorf("Binance funding interval should be 8h, got %v", tk.IntervalH)
		}
	}
}

func TestParseWS_TickerStreamVolumeOnly(t *testing.T) {
	a := New()
	// !ticker@arr — provides 24h quote volume (q field). Rate/mark missing.
	frame := []byte(`{"stream":"!ticker@arr","data":[
		{"s":"BTCUSDT","q":"1000000000"},
		{"s":"ETHUSDT","q":"500000000"}
	]}`)
	ticks, _ := a.ParseWS(frame)
	if len(ticks) != 2 {
		t.Fatalf("len: %d", len(ticks))
	}
	if ticks[0].Symbol != "BTC" || ticks[0].Volume24h != 1000000000 {
		t.Errorf("BTC volume: %+v", ticks[0])
	}
	if ticks[0].Rate != 0 {
		t.Errorf("ticker stream should NOT populate rate, got %v", ticks[0].Rate)
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
	frame := []byte(`{"stream":"btcusdt@kline_1m","data":{}}`)
	ticks, _ := a.ParseWS(frame)
	if ticks != nil {
		t.Errorf("unknown stream should produce nil, got %v", ticks)
	}
}

func TestParseWS_VolumeZeroFiltered(t *testing.T) {
	a := New()
	// 0-volume rows are filtered from ticker stream (low-quality data)
	frame := []byte(`{"stream":"!ticker@arr","data":[
		{"s":"BTCUSDT","q":"0"},
		{"s":"ETHUSDT","q":"500000"}
	]}`)
	ticks, _ := a.ParseWS(frame)
	if len(ticks) != 1 || ticks[0].Symbol != "ETH" {
		t.Errorf("zero-volume filtered: got %v", ticks)
	}
}
