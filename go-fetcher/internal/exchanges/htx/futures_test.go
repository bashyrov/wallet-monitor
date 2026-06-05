package htx

import (
	"errors"
	"testing"

	"github.com/rs/zerolog"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

func newTestFutures() *Futures {
	return &Futures{
		books: make(map[string]*book),
		bbo:   make(map[string]*bboLevel),
		log:   zerolog.Nop(),
	}
}

func TestParse_SnapshotEstablishesVersion(t *testing.T) {
	a := newTestFutures()
	frame := []byte(`{"ch":"market.BTC-USDT.depth.size_20.high_freq","tick":{"event":"snapshot","version":1000,"bids":[[60000,1.5]],"asks":[[60100,2.0]]}}`)
	snap, err := a.Parse(frame)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if snap == nil {
		t.Fatal("expected snapshot, got nil")
	}
	if snap.Symbol != "BTC" {
		t.Errorf("symbol: want BTC got %q", snap.Symbol)
	}
	if got := a.books["BTC"].lastVersion; got != 1000 {
		t.Errorf("lastVersion: want 1000 got %d", got)
	}
}

func TestParse_InOrderUpdateAdvancesVersion(t *testing.T) {
	a := newTestFutures()
	_, _ = a.Parse([]byte(`{"ch":"market.ETH-USDT.depth.size_20.high_freq","tick":{"event":"snapshot","version":500,"bids":[[3000,5]],"asks":[]}}`))
	_, err := a.Parse([]byte(`{"ch":"market.ETH-USDT.depth.size_20.high_freq","tick":{"event":"update","version":501,"bids":[[2999,10]],"asks":[]}}`))
	if err != nil {
		t.Fatalf("parse update: %v", err)
	}
	if got := a.books["ETH"].lastVersion; got != 501 {
		t.Errorf("lastVersion after in-order update: want 501 got %d", got)
	}
}

func TestParse_GapReturnsErrResync(t *testing.T) {
	// On version gap, Parse must return ws.ErrResync and clear the book state
	// so the runner reconnects for a fresh snapshot.
	a := newTestFutures()
	_, _ = a.Parse([]byte(`{"ch":"market.SOL-USDT.depth.size_20.high_freq","tick":{"event":"snapshot","version":200,"bids":[[150,3]],"asks":[]}}`))
	// jump from 200 → 205 (gap of 4)
	snap, err := a.Parse([]byte(`{"ch":"market.SOL-USDT.depth.size_20.high_freq","tick":{"event":"update","version":205,"bids":[[151,2]],"asks":[]}}`))
	if !errors.Is(err, ws.ErrResync) {
		t.Errorf("version gap must return ws.ErrResync, got err=%v snap=%v", err, snap)
	}
	bk := a.books["SOL"]
	if bk == nil {
		t.Fatal("book entry should still exist (just cleared)")
	}
	if len(bk.bids) != 0 || len(bk.asks) != 0 || bk.lastVersion != 0 {
		t.Errorf("book not cleared after gap: bids=%d asks=%d ver=%d", len(bk.bids), len(bk.asks), bk.lastVersion)
	}
}

func TestParse_FirstUpdateBeforeSnapshotNoWarn(t *testing.T) {
	a := newTestFutures()
	_, err := a.Parse([]byte(`{"ch":"market.BTC-USDT.depth.size_20.high_freq","tick":{"event":"update","version":9999,"bids":[[60000,1]],"asks":[]}}`))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if got := a.books["BTC"].lastVersion; got != 9999 {
		t.Errorf("lastVersion: want 9999 got %d", got)
	}
}

func TestParse_NonDepthChannelsIgnored(t *testing.T) {
	a := newTestFutures()
	// e.g. server pong reply — no `ch` matching depth pattern
	snap, err := a.Parse([]byte(`{"op":"pong","ts":12345}`))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if snap != nil {
		t.Errorf("expected nil for non-depth frame, got %+v", snap)
	}
}
