package okx

import (
	"testing"
)

func TestParseWS_FundingRateChannel(t *testing.T) {
	a := New()
	frame := []byte(`{"arg":{"channel":"funding-rate","instId":"BTC-USDT-SWAP"},"data":[{"fundingRate":"0.0001","nextFundingTime":"1718000028000"}]}`)
	ticks, err := a.ParseWS(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(ticks) != 1 {
		t.Fatalf("len: %d", len(ticks))
	}
	tk := ticks[0]
	if tk.Symbol != "BTC" {
		t.Errorf("symbol: %s", tk.Symbol)
	}
	if tk.Rate != 0.0001 {
		t.Errorf("rate: %v", tk.Rate)
	}
	if tk.NextFunding.UnixMilli() != 1718000028000 {
		t.Errorf("next funding: %v", tk.NextFunding)
	}
}

func TestParseWS_TickersChannelConvertsBaseVolumeToUSD(t *testing.T) {
	a := New()
	// volCcy24h is in BASE units (e.g. 10000 BTC); converted via last price
	frame := []byte(`{"arg":{"channel":"tickers","instId":"BTC-USDT-SWAP"},"data":[{"last":"60000","idxPx":"60050","volCcy24h":"10000"}]}`)
	ticks, _ := a.ParseWS(frame)
	if len(ticks) != 1 {
		t.Fatalf("ticks: %d", len(ticks))
	}
	tk := ticks[0]
	if tk.MarkPrice != 60000 {
		t.Errorf("mark from last: %v", tk.MarkPrice)
	}
	// 10000 BTC × 60000 USD/BTC = 600M USD
	if tk.Volume24h != 6e8 {
		t.Errorf("volume conversion: want 6e8 got %v", tk.Volume24h)
	}
}

func TestParseWS_EventFrameIgnored(t *testing.T) {
	a := New()
	ticks, _ := a.ParseWS([]byte(`{"event":"subscribe","arg":{"channel":"funding-rate","instId":"BTC-USDT-SWAP"}}`))
	if ticks != nil {
		t.Errorf("event frame should produce nil, got %v", ticks)
	}
}

func TestParseWS_NonSWAPInstFiltered(t *testing.T) {
	a := New()
	// Spot or margin instId — different suffix
	ticks, _ := a.ParseWS([]byte(`{"arg":{"channel":"tickers","instId":"BTC-USDT"},"data":[{"last":"60000"}]}`))
	if ticks != nil {
		t.Errorf("non-SWAP should produce nil, got %v", ticks)
	}
}

func TestParseWS_UnknownChannelDoesNotPopulate(t *testing.T) {
	a := New()
	// Channel outside the known set is silently skipped — tick is created
	// but not appended (continue in switch default)
	frame := []byte(`{"arg":{"channel":"index-tickers","instId":"BTC-USDT-SWAP"},"data":[{"idxPx":"60000"}]}`)
	ticks, _ := a.ParseWS(frame)
	if len(ticks) != 0 {
		t.Errorf("unknown channel should yield 0 ticks, got %v", ticks)
	}
}
