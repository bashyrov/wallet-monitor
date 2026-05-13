package gate

import (
	"testing"
)

func TestParseWS_UpdateEvent(t *testing.T) {
	a := New()
	frame := []byte(`{"channel":"futures.tickers","event":"update","result":[
		{"contract":"BTC_USDT","mark_price":"60000.5","index_price":"60000","funding_rate":"0.0001","volume_24h_usd":"1000000000"},
		{"contract":"ETH_USDT","mark_price":"3000","index_price":"2999.5","funding_rate":"-0.0002","volume_24h_usd":"500000000"}
	]}`)
	ticks, err := a.ParseWS(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(ticks) != 2 {
		t.Fatalf("len: %d", len(ticks))
	}
	if ticks[0].Symbol != "BTC" || ticks[0].MarkPrice != 60000.5 || ticks[0].Rate != 0.0001 {
		t.Errorf("BTC: %+v", ticks[0])
	}
	if ticks[1].Symbol != "ETH" || ticks[1].Rate != -0.0002 {
		t.Errorf("ETH (negative rate): %+v", ticks[1])
	}
	if ticks[0].Volume24h != 1e9 {
		t.Errorf("volume_24h_usd: %v", ticks[0].Volume24h)
	}
}

func TestParseWS_VolumeFallbackChain(t *testing.T) {
	a := New()
	// First row: volume_24h_usd present
	// Second row: volume_24h_usd absent, volume_24h_quote present
	// Third row: only volume_24h_settle (legacy)
	frame := []byte(`{"channel":"futures.tickers","event":"update","result":[
		{"contract":"BTC_USDT","mark_price":"60000","funding_rate":"0","volume_24h_usd":"1000"},
		{"contract":"ETH_USDT","mark_price":"3000","funding_rate":"0","volume_24h_quote":"500"},
		{"contract":"SOL_USDT","mark_price":"150","funding_rate":"0","volume_24h_settle":"200"}
	]}`)
	ticks, _ := a.ParseWS(frame)
	if len(ticks) != 3 {
		t.Fatalf("len: %d", len(ticks))
	}
	if ticks[0].Volume24h != 1000 || ticks[1].Volume24h != 500 || ticks[2].Volume24h != 200 {
		t.Errorf("fallback chain: %v / %v / %v",
			ticks[0].Volume24h, ticks[1].Volume24h, ticks[2].Volume24h)
	}
}

func TestParseWS_NonUSDTContractFiltered(t *testing.T) {
	a := New()
	ticks, _ := a.ParseWS([]byte(`{"channel":"futures.tickers","event":"update","result":[
		{"contract":"BTC_USDC","mark_price":"60000","funding_rate":"0.0001"}
	]}`))
	if len(ticks) != 0 {
		t.Errorf("non-_USDT should be filtered, got %v", ticks)
	}
}

func TestParseWS_NonUpdateEventIgnored(t *testing.T) {
	a := New()
	ticks, _ := a.ParseWS([]byte(`{"channel":"futures.tickers","event":"subscribe","result":{"status":"success"}}`))
	if ticks != nil {
		t.Errorf("subscribe event should produce nil, got %v", ticks)
	}
}

func TestParseWS_NonTickersChannelIgnored(t *testing.T) {
	a := New()
	ticks, _ := a.ParseWS([]byte(`{"channel":"futures.order_book","event":"update","result":[]}`))
	if ticks != nil {
		t.Errorf("non-tickers channel should produce nil, got %v", ticks)
	}
}

func TestParseWS_IntervalHNotSet(t *testing.T) {
	// Per code comment: Gate's WS payload doesn't carry funding interval,
	// so it's left at 0 to let the REST backstop fill it in.
	a := New()
	ticks, _ := a.ParseWS([]byte(`{"channel":"futures.tickers","event":"update","result":[
		{"contract":"BTC_USDT","mark_price":"60000","funding_rate":"0"}
	]}`))
	if len(ticks) != 1 {
		t.Fatalf("len: %d", len(ticks))
	}
	if ticks[0].IntervalH != 0 {
		t.Errorf("WS shouldn't force IntervalH (let REST set it), got %v", ticks[0].IntervalH)
	}
}
