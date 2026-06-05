package kucoin

import (
	"strings"
	"testing"
)

// ── BTC ↔ XBT aliasing ────────────────────────────────────────────────

func TestTokenContractAliasing(t *testing.T) {
	if tokenToContract("BTC") != "XBTUSDTM" {
		t.Errorf("BTC → XBTUSDTM, got %s", tokenToContract("BTC"))
	}
	if tokenToContract("ETH") != "ETHUSDTM" {
		t.Errorf("ETH → ETHUSDTM, got %s", tokenToContract("ETH"))
	}
	if contractToToken("XBTUSDTM") != "BTC" {
		t.Errorf("XBTUSDTM → BTC")
	}
	if contractToToken("ETHUSDTM") != "ETH" {
		t.Errorf("ETHUSDTM → ETH")
	}
}

// ── BuildSubscribe uses level2Depth50 and XBT alias ───────────────────

func TestBuildSubscribe_TargetsDepth5(t *testing.T) {
	a := &Futures{auth: &authClient{}}
	frames := a.BuildSubscribe([]string{"BTC", "ETH"})
	if len(frames) != 2 {
		t.Fatalf("expected 2 frames, got %d", len(frames))
	}
	if !strings.Contains(string(frames[0]), "/contractMarket/level2Depth50:XBTUSDTM") {
		t.Errorf("BTC → XBT alias + Depth5 missing: %s", frames[0])
	}
	if !strings.Contains(string(frames[1]), "/contractMarket/level2Depth50:ETHUSDTM") {
		t.Errorf("ETH Depth5 missing: %s", frames[1])
	}
	if strings.Contains(string(frames[0]), "level2:") {
		t.Errorf("must NOT use raw level2 channel")
	}
}

// ── Parse happy path (price string, size number — real KuCoin format) ───

func TestParse_Depth5Snapshot(t *testing.T) {
	a := &Futures{auth: &authClient{}}
	// Real wire format: price = string, size = number (not string)
	frame := []byte(`{"type":"message","topic":"/contractMarket/level2Depth50:XBTUSDTM","subject":"level2Depth50Snapshot","data":{"bids":[["64000.0",1.5],["63999.0",0.8]],"asks":[["64001.0",2.1],["64002.0",0.5]],"ts":1624963009327,"sequence":123}}`)
	snap, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if snap == nil {
		t.Fatal("expected non-nil snapshot")
	}
	if snap.Symbol != "BTC" {
		t.Errorf("symbol: want BTC, got %s", snap.Symbol)
	}
	if len(snap.Bids) != 2 || snap.Bids[0][0] != 64000.0 || snap.Bids[0][1] != 1.5 {
		t.Errorf("bids wrong: %+v", snap.Bids)
	}
	if len(snap.Asks) != 2 || snap.Asks[0][0] != 64001.0 || snap.Asks[0][1] != 2.1 {
		t.Errorf("asks wrong: %+v", snap.Asks)
	}
	if snap.EventTime.IsZero() {
		t.Error("expected non-zero EventTime from ts field")
	}
}

// ── Both string and number sizes work ────────────────────────────────────

func TestParse_Depth5With3ElementRows(t *testing.T) {
	a := &Futures{auth: &authClient{}}
	// 3-element rows: [price_str, size_num, numOrders_num]
	frame := []byte(`{"type":"message","topic":"/contractMarket/level2Depth50:XBTUSDTM","data":{"bids":[["64000",2,5]],"asks":[["64001",1,3]],"ts":1000}}`)
	snap, err := a.Parse(frame)
	if err != nil || snap == nil {
		t.Fatalf("parse failed: err=%v snap=%v", err, snap)
	}
	if snap.Bids[0][1] != 2 || snap.Asks[0][1] != 1 {
		t.Errorf("wrong size from 3-element row: bids=%v asks=%v", snap.Bids, snap.Asks)
	}
}

// ── Size as string also works (defensive) ────────────────────────────────

func TestParse_Depth5SizeAsString(t *testing.T) {
	a := &Futures{auth: &authClient{}}
	frame := []byte(`{"type":"message","topic":"/contractMarket/level2Depth50:ETHUSDTM","data":{"bids":[["2000","3.5"]],"asks":[["2001","1.2"]],"ts":1000}}`)
	snap, err := a.Parse(frame)
	if err != nil || snap == nil {
		t.Fatalf("parse failed: err=%v snap=%v", err, snap)
	}
	if snap.Bids[0][1] != 3.5 || snap.Asks[0][1] != 1.2 {
		t.Errorf("string sizes wrong: bids=%v asks=%v", snap.Bids, snap.Asks)
	}
}

// ── Zero-size levels filtered out ─────────────────────────────────────

func TestParse_ZeroSizeLevelsDropped(t *testing.T) {
	a := &Futures{auth: &authClient{}}
	frame := []byte(`{"type":"message","topic":"/contractMarket/level2Depth50:ETHUSDTM","data":{"bids":[["2000",0],["1999",1]],"asks":[["2001",0.5]],"ts":1000}}`)
	snap, err := a.Parse(frame)
	if err != nil || snap == nil {
		t.Fatal(err)
	}
	if len(snap.Bids) != 1 || snap.Bids[0][0] != 1999 {
		t.Errorf("zero-size bid should be dropped, got %v", snap.Bids)
	}
}

// ── Non-matching type/topic → nil ─────────────────────────────────────

func TestParse_WrongTypeIgnored(t *testing.T) {
	a := &Futures{auth: &authClient{}}
	// pong frame
	got, _ := a.Parse([]byte(`{"type":"pong","id":"123"}`))
	if got != nil {
		t.Error("pong must be ignored")
	}
	// wrong topic (level2: not level2Depth50:)
	got, _ = a.Parse([]byte(`{"type":"message","topic":"/contractMarket/level2:XBTUSDTM","data":{"bids":[],"asks":[],"ts":1}}`))
	if got != nil {
		t.Error("level2 raw channel must be ignored")
	}
	// ack frame
	got, _ = a.Parse([]byte(`{"type":"ack","id":"1"}`))
	if got != nil {
		t.Error("ack must be ignored")
	}
}

// ── ETH symbol round-trip ─────────────────────────────────────────────

func TestParse_ETHSymbol(t *testing.T) {
	a := &Futures{auth: &authClient{}}
	frame := []byte(`{"type":"message","topic":"/contractMarket/level2Depth50:ETHUSDTM","data":{"bids":[["2000","1"]],"asks":[["2001","2"]],"ts":9999}}`)
	snap, err := a.Parse(frame)
	if err != nil || snap == nil {
		t.Fatal(err)
	}
	if snap.Symbol != "ETH" {
		t.Errorf("expected ETH, got %s", snap.Symbol)
	}
}
