package kraken

import (
	"errors"
	"testing"

	"github.com/rs/zerolog"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

func newTestFutures() *Futures {
	return &Futures{
		books: make(map[string]*book),
		log:   zerolog.Nop(),
	}
}

func TestParse_SnapshotEstablishesSeq(t *testing.T) {
	a := newTestFutures()
	frame := []byte(`{"feed":"book_snapshot","product_id":"PF_XBTUSD","seq":100,"bids":[{"price":60000,"qty":1.5}],"asks":[{"price":60100,"qty":2.0}]}`)
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
	if got := a.books["BTC"].lastSeq; got != 100 {
		t.Errorf("lastSeq: want 100 got %d", got)
	}
}

func TestParse_InOrderDeltaAdvancesSeq(t *testing.T) {
	a := newTestFutures()
	// snapshot @ seq=100
	_, _ = a.Parse([]byte(`{"feed":"book_snapshot","product_id":"PF_ETHUSD","seq":100,"bids":[{"price":3000,"qty":5}],"asks":[]}`))
	// delta @ seq=101 — in-order
	_, err := a.Parse([]byte(`{"feed":"book","product_id":"PF_ETHUSD","seq":101,"side":"buy","price":2999,"qty":10}`))
	if err != nil {
		t.Fatalf("parse delta: %v", err)
	}
	if got := a.books["ETH"].lastSeq; got != 101 {
		t.Errorf("lastSeq after in-order delta: want 101 got %d", got)
	}
}

func TestParse_GapReturnsErrResync(t *testing.T) {
	// On seq gap, Parse must return ws.ErrResync and clear the book state
	// so the runner reconnects for a fresh book_snapshot.
	a := newTestFutures()
	_, _ = a.Parse([]byte(`{"feed":"book_snapshot","product_id":"PF_SOLUSD","seq":50,"bids":[{"price":150,"qty":3}],"asks":[]}`))
	// jump from 50 → 55 (gap of 4 missed)
	snap, err := a.Parse([]byte(`{"feed":"book","product_id":"PF_SOLUSD","seq":55,"side":"sell","price":151,"qty":2}`))
	if !errors.Is(err, ws.ErrResync) {
		t.Errorf("gap must return ws.ErrResync, got err=%v snap=%v", err, snap)
	}
	// Book state must be cleared so reconnect starts clean.
	bk := a.books["SOL"]
	if bk == nil {
		t.Fatal("book entry should still exist (just cleared)")
	}
	if len(bk.bids) != 0 || len(bk.asks) != 0 || bk.lastSeq != 0 {
		t.Errorf("book not cleared after gap: bids=%d asks=%d seq=%d", len(bk.bids), len(bk.asks), bk.lastSeq)
	}
}

func TestParse_FirstDeltaBeforeSnapshotNoWarn(t *testing.T) {
	// Edge case: very first frame is a delta (no snapshot seen yet).
	// lastSeq starts at 0; the gap guard `lastSeq != 0` suppresses the
	// warn until we have a baseline.
	a := newTestFutures()
	_, err := a.Parse([]byte(`{"feed":"book","product_id":"PF_XBTUSD","seq":999,"side":"buy","price":60000,"qty":1}`))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if got := a.books["BTC"].lastSeq; got != 999 {
		t.Errorf("lastSeq: want 999 got %d", got)
	}
}

func TestParse_XBTAliasMapsToBTC(t *testing.T) {
	a := newTestFutures()
	snap, _ := a.Parse([]byte(`{"feed":"book_snapshot","product_id":"PF_XBTUSD","seq":1,"bids":[],"asks":[]}`))
	if snap.Symbol != "BTC" {
		t.Errorf("XBT should alias to BTC, got %q", snap.Symbol)
	}
}

func TestParse_NonProductFramesIgnored(t *testing.T) {
	a := newTestFutures()
	// info / subscribed events have an Event field set
	snap, err := a.Parse([]byte(`{"event":"subscribed","feed":"book","product_id":"PF_XBTUSD"}`))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if snap != nil {
		t.Errorf("expected nil for subscribed event, got %+v", snap)
	}
}
