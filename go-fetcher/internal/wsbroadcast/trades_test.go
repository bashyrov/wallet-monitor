package wsbroadcast

import (
	"encoding/json"
	"testing"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
)

func newTestTrades() *Trades {
	return NewTrades(ticks.NewRing(50), nil)
}

func TestTrades_HandleSubscribe_NormalizesAndStoresPairs(t *testing.T) {
	tt := newTestTrades()
	c := &client{outbox: make(chan []byte, 8), done: make(chan struct{})}
	tt.subs[c] = make(map[string]struct{}, 4)

	tt.handleSubscribe(c, []string{"binance:BTC", "BYBIT:eth"})
	if _, ok := tt.subs[c]["binance:BTC"]; !ok {
		t.Errorf("binance:BTC not subscribed")
	}
	if _, ok := tt.subs[c]["bybit:ETH"]; !ok {
		t.Errorf("normalize BYBIT:eth → bybit:ETH failed")
	}
}

func TestTrades_HandleSubscribe_CapAt100(t *testing.T) {
	tt := newTestTrades()
	c := &client{outbox: make(chan []byte, 8), done: make(chan struct{})}
	tt.subs[c] = make(map[string]struct{}, 4)

	pairs := make([]string, 150)
	for i := range pairs {
		pairs[i] = "binance:SYM" + intToStr(i)
	}
	tt.handleSubscribe(c, pairs)

	if got := len(tt.subs[c]); got > tradesMaxPairsPerClient {
		t.Errorf("cap violated: got %d max %d", got, tradesMaxPairsPerClient)
	}
}

func TestTrades_HandleSubscribe_DedupesExisting(t *testing.T) {
	tt := newTestTrades()
	c := &client{outbox: make(chan []byte, 8), done: make(chan struct{})}
	tt.subs[c] = make(map[string]struct{}, 4)

	tt.handleSubscribe(c, []string{"binance:BTC", "binance:ETH"})
	tt.handleSubscribe(c, []string{"binance:BTC", "binance:SOL"})

	if len(tt.subs[c]) != 3 {
		t.Errorf("dedup: want 3 entries got %d", len(tt.subs[c]))
	}
}

func TestTrades_HandleUnsubscribe(t *testing.T) {
	tt := newTestTrades()
	c := &client{outbox: make(chan []byte, 8), done: make(chan struct{})}
	tt.subs[c] = make(map[string]struct{}, 4)

	tt.handleSubscribe(c, []string{"binance:BTC", "binance:ETH"})
	tt.handleUnsubscribe(c, []string{"binance:BTC"})

	if _, ok := tt.subs[c]["binance:BTC"]; ok {
		t.Errorf("BTC still subscribed after unsubscribe")
	}
	if _, ok := tt.subs[c]["binance:ETH"]; !ok {
		t.Errorf("ETH should remain")
	}
}

func TestTrades_OnTick_PushesToSubscribedClient(t *testing.T) {
	tt := newTestTrades()
	c := &client{outbox: make(chan []byte, 8), done: make(chan struct{})}
	tt.subs[c] = map[string]struct{}{"binance:BTC": {}}

	tk := ticks.Tick{Exchange: "binance", Symbol: "BTC", Price: 60000, Size: 1, Side: ticks.Buy, TsMS: 1, ID: "x"}
	tt.OnTick(tk)

	select {
	case body := <-c.outbox:
		var decoded struct {
			Trades []ticks.Tick `json:"trades"`
		}
		if err := json.Unmarshal(body, &decoded); err != nil {
			t.Fatalf("decode: %v", err)
		}
		if len(decoded.Trades) != 1 || decoded.Trades[0].Price != 60000 {
			t.Errorf("payload mismatch: %+v", decoded.Trades)
		}
	default:
		t.Errorf("OnTick produced no outbox message")
	}
}

func TestTrades_OnTick_SkipsUnsubscribedClient(t *testing.T) {
	tt := newTestTrades()
	c := &client{outbox: make(chan []byte, 8), done: make(chan struct{})}
	tt.subs[c] = map[string]struct{}{"binance:ETH": {}} // subscribed to ETH only

	tt.OnTick(ticks.Tick{Exchange: "binance", Symbol: "BTC", Price: 60000, Size: 1})

	select {
	case body := <-c.outbox:
		t.Errorf("got unexpected outbox message: %s", body)
	default:
		// ok
	}
}

func TestTrades_OnTick_PushesToRingForBackfill(t *testing.T) {
	ring := ticks.NewRing(50)
	tt := NewTrades(ring, nil)
	tk := ticks.Tick{Exchange: "binance", Symbol: "BTC", Price: 60000, Size: 1, ID: "x"}
	tt.OnTick(tk)
	recent := ring.Recent("binance", "BTC", 0)
	if len(recent) != 1 || recent[0].ID != "x" {
		t.Errorf("ring should hold the tick: %+v", recent)
	}
}

func TestTrades_HandleSubscribe_BackfillsFromRing(t *testing.T) {
	ring := ticks.NewRing(50)
	// Seed ring with prior trades
	ring.Push(ticks.Tick{Exchange: "binance", Symbol: "BTC", Price: 59000, Size: 1, ID: "old1"})
	ring.Push(ticks.Tick{Exchange: "binance", Symbol: "BTC", Price: 59500, Size: 1, ID: "old2"})
	tt := NewTrades(ring, nil)
	c := &client{outbox: make(chan []byte, 8), done: make(chan struct{})}
	tt.subs[c] = make(map[string]struct{}, 4)

	tt.handleSubscribe(c, []string{"binance:BTC"})

	select {
	case body := <-c.outbox:
		var decoded struct{ Trades []ticks.Tick `json:"trades"` }
		_ = json.Unmarshal(body, &decoded)
		if len(decoded.Trades) != 2 {
			t.Errorf("backfill: want 2 trades got %d", len(decoded.Trades))
		}
	default:
		t.Errorf("subscribe should backfill from ring")
	}
}
