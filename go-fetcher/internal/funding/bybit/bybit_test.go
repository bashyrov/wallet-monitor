package bybit

import (
	"testing"
)

func TestParseWS_TickerSnapshot(t *testing.T) {
	a := New()
	frame := []byte(`{"topic":"tickers.BTCUSDT","type":"snapshot","data":{
		"symbol":"BTCUSDT","fundingRate":"0.0001","markPrice":"60000.5",
		"indexPrice":"60000.0","nextFundingTime":"1718000028000","turnover24h":"1000000000"
	}}`)
	ticks, err := a.ParseWS(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(ticks) != 1 {
		t.Fatalf("ticks: %d", len(ticks))
	}
	tk := ticks[0]
	if tk.Symbol != "BTC" {
		t.Errorf("symbol: %s", tk.Symbol)
	}
	if tk.Rate != 0.0001 || tk.MarkPrice != 60000.5 || tk.IndexPrice != 60000.0 {
		t.Errorf("rate/mark/idx: %+v", tk)
	}
	if tk.Volume24h != 1000000000 {
		t.Errorf("turnover24h → Volume24h: %v", tk.Volume24h)
	}
	if tk.NextFunding.UnixMilli() != 1718000028000 {
		t.Errorf("nextFundingTime: %v", tk.NextFunding)
	}
	if tk.IntervalH != 8 {
		t.Errorf("interval: %v", tk.IntervalH)
	}
}

func TestParseWS_OpFrameIgnored(t *testing.T) {
	a := New()
	// Subscribe ack
	ticks, _ := a.ParseWS([]byte(`{"op":"subscribe","success":true}`))
	if ticks != nil {
		t.Errorf("op frame should produce nil, got %v", ticks)
	}
}

func TestParseWS_NonTickersTopicIgnored(t *testing.T) {
	a := New()
	ticks, _ := a.ParseWS([]byte(`{"topic":"orderbook.50.BTCUSDT","data":{"symbol":"BTCUSDT","fundingRate":"0.0001"}}`))
	if ticks != nil {
		t.Errorf("non-tickers topic should produce nil, got %v", ticks)
	}
}

func TestParseWS_NonUSDTFiltered(t *testing.T) {
	a := New()
	ticks, _ := a.ParseWS([]byte(`{"topic":"tickers.BTCUSDC","data":{"symbol":"BTCUSDC","fundingRate":"0.0001"}}`))
	if ticks != nil {
		t.Errorf("non-USDT should produce nil, got %v", ticks)
	}
}

func TestParseWS_DeltaUpdate(t *testing.T) {
	a := New()
	// Bybit also sends type="delta" with partial fields — adapter should still parse
	frame := []byte(`{"topic":"tickers.ETHUSDT","type":"delta","data":{"symbol":"ETHUSDT","fundingRate":"-0.0002","markPrice":"3000"}}`)
	ticks, _ := a.ParseWS(frame)
	if len(ticks) != 1 || ticks[0].Symbol != "ETH" || ticks[0].Rate != -0.0002 {
		t.Errorf("delta: %v", ticks)
	}
}
