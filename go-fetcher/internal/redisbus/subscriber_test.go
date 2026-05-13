package redisbus

import (
	"context"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/symbols"
)

func TestNewSubscriber_EmptyURLReturnsNil(t *testing.T) {
	s, err := NewSubscriber("", symbols.New())
	if err != nil {
		t.Errorf("empty URL should not error, got %v", err)
	}
	if s != nil {
		t.Errorf("empty URL should return nil subscriber")
	}
}

func TestNewSubscriber_InvalidURLReturnsError(t *testing.T) {
	s, err := NewSubscriber("not-a-url", symbols.New())
	if err == nil {
		t.Errorf("invalid URL should error")
	}
	if s != nil {
		t.Errorf("invalid URL should not return subscriber")
	}
}

func TestSubscriber_Run_NilSafe(t *testing.T) {
	var s *Subscriber
	// Must not panic — Run() returns immediately on nil receiver.
	s.Run(context.Background())
}

func TestSubscriber_Close_NilSafe(t *testing.T) {
	var s *Subscriber
	if err := s.Close(); err != nil {
		t.Errorf("nil Close should be safe, got %v", err)
	}
}

func TestSubscriber_PublishesRoutedToManagerTouch(t *testing.T) {
	mr := miniredis.RunT(t)
	mgr := symbols.New()
	s, err := NewSubscriber("redis://"+mr.Addr(), mgr)
	if err != nil {
		t.Fatalf("NewSubscriber: %v", err)
	}
	defer s.Close()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go s.Run(ctx)

	// Allow subscribe handshake to complete
	time.Sleep(50 * time.Millisecond)

	// Publish to book:subscribe — should land in mgr.Touch
	pub := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	defer pub.Close()
	if err := pub.Publish(ctx, "book:subscribe", "binance:BTC").Err(); err != nil {
		t.Fatalf("publish: %v", err)
	}

	// Give subscriber time to process
	deadline := time.Now().Add(500 * time.Millisecond)
	for time.Now().Before(deadline) {
		if mgr.HasUserSub("binance", "BTC") {
			return // pass
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Errorf("publish to book:subscribe didn't reach mgr.Touch")
}

func TestSubscriber_UnsubscribeRoutedToManagerUntouch(t *testing.T) {
	mr := miniredis.RunT(t)
	mgr := symbols.New()
	mgr.Touch("binance", "BTC")

	s, _ := NewSubscriber("redis://"+mr.Addr(), mgr)
	defer s.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go s.Run(ctx)
	time.Sleep(50 * time.Millisecond)

	pub := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	defer pub.Close()
	_ = pub.Publish(ctx, "book:unsubscribe", "binance:BTC").Err()

	deadline := time.Now().Add(500 * time.Millisecond)
	for time.Now().Before(deadline) {
		if !mgr.HasUserSub("binance", "BTC") {
			return // pass: touch was removed
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Errorf("publish to book:unsubscribe didn't reach mgr.Untouch")
}

func TestSplitPair_Valid(t *testing.T) {
	cases := []struct{ in, ex, sym string }{
		{"binance:BTC", "binance", "BTC"},
		{"BYBIT:eth", "bybit", "ETH"},
		{"  binance  :  BTC  ", "binance", "BTC"},
		{"hyperliquid:SOL_USD", "hyperliquid", "SOL_USD"},
	}
	for _, c := range cases {
		ex, sym := splitPair(c.in)
		if ex != c.ex || sym != c.sym {
			t.Errorf("splitPair(%q): want (%q,%q) got (%q,%q)", c.in, c.ex, c.sym, ex, sym)
		}
	}
}

func TestSplitPair_Invalid(t *testing.T) {
	cases := []string{
		"",
		":",
		":BTC",
		"binance:",
		"binance",
		"binance/BTC",        // / not alnum_
		"binance:BTC-USDT",   // dash rejected
		"binance:BTC.USD",    // dot rejected
	}
	for _, c := range cases {
		ex, sym := splitPair(c)
		if ex != "" || sym != "" {
			t.Errorf("splitPair(%q): expected empty, got (%q,%q)", c, ex, sym)
		}
	}
}

func TestAlnum(t *testing.T) {
	cases := []struct {
		r    rune
		want bool
	}{
		{'a', true}, {'A', true}, {'0', true}, {'9', true},
		{'_', false}, {'-', false}, {'.', false}, {' ', false},
	}
	for _, c := range cases {
		if got := alnum(c.r); got != c.want {
			t.Errorf("alnum(%q): want %v got %v", c.r, c.want, got)
		}
	}
}
