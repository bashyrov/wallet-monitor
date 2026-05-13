package bitget

import (
	"testing"
)

func TestParseWS_TickerData(t *testing.T) {
	a := New()
	frame := []byte(`{"arg":{"instType":"USDT-FUTURES","channel":"ticker","instId":"BTCUSDT"},"data":[{
		"instId":"BTCUSDT","lastPr":"60000","indexPrice":"60050","markPrice":"60050.5",
		"fundingRate":"0.0001","nextFundingTime":"1718000028000","quoteVolume":"1000000000"
	}]}`)
	ticks, err := a.ParseWS(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(ticks) != 1 {
		t.Fatalf("len: %d", len(ticks))
	}
	tk := ticks[0]
	if tk.Symbol != "BTC" || tk.MarkPrice != 60050.5 || tk.Rate != 0.0001 {
		t.Errorf("primary fields: %+v", tk)
	}
	if tk.Volume24h != 1e9 {
		t.Errorf("quoteVolume: %v", tk.Volume24h)
	}
	if tk.NextFunding.UnixMilli() != 1718000028000 {
		t.Errorf("nextFundingTime: %v", tk.NextFunding)
	}
}

func TestParseWS_MarkPriceFallbackToLastPr(t *testing.T) {
	a := New()
	// markPrice absent — adapter falls back to lastPr
	frame := []byte(`{"arg":{"channel":"ticker","instId":"ETHUSDT"},"data":[{"instId":"ETHUSDT","lastPr":"3000","fundingRate":"0"}]}`)
	ticks, _ := a.ParseWS(frame)
	if len(ticks) != 1 {
		t.Fatalf("len: %d", len(ticks))
	}
	if ticks[0].MarkPrice != 3000 {
		t.Errorf("markPrice fallback: %v", ticks[0].MarkPrice)
	}
}

func TestParseWS_EventFrameIgnored(t *testing.T) {
	a := New()
	ticks, _ := a.ParseWS([]byte(`{"event":"subscribe","arg":{"channel":"ticker","instId":"BTCUSDT"}}`))
	if ticks != nil {
		t.Errorf("event frame should produce nil, got %v", ticks)
	}
}

func TestParseWS_NonTickerChannelIgnored(t *testing.T) {
	a := New()
	ticks, _ := a.ParseWS([]byte(`{"arg":{"channel":"books15","instId":"BTCUSDT"},"data":[]}`))
	if ticks != nil {
		t.Errorf("non-ticker channel should produce nil, got %v", ticks)
	}
}

func TestParseWS_NonUSDTFiltered(t *testing.T) {
	a := New()
	ticks, _ := a.ParseWS([]byte(`{"arg":{"channel":"ticker","instId":"BTCUSDC"},"data":[{"instId":"BTCUSDC","markPrice":"60000","fundingRate":"0.0001"}]}`))
	if len(ticks) != 0 {
		t.Errorf("non-USDT should be filtered, got %v", ticks)
	}
}

func TestParseWS_IntervalHNotSet(t *testing.T) {
	// Per code comment: WS doesn't carry per-pair interval, leave at 0
	// to let REST backstop fill it in. Forcing 8 would wipe 4h pairs.
	a := New()
	ticks, _ := a.ParseWS([]byte(`{"arg":{"channel":"ticker","instId":"BTCUSDT"},"data":[{"instId":"BTCUSDT","markPrice":"60000","fundingRate":"0"}]}`))
	if len(ticks) != 1 {
		t.Fatalf("len: %d", len(ticks))
	}
	if ticks[0].IntervalH != 0 {
		t.Errorf("WS shouldn't set IntervalH, got %v", ticks[0].IntervalH)
	}
}
