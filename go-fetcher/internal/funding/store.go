package funding

import (
	"sync"
	"time"
)

// Store is the in-memory funding cache. One entry per (exchange, symbol).
// Concurrent-safe — many WS adapters + REST goroutines write; one dumper
// reads.
type Store struct {
	mu     sync.RWMutex
	ticks  map[string]*Tick // key = "<exchange>:<symbol>"
}

func NewStore() *Store {
	return &Store{ticks: make(map[string]*Tick, 1024)}
}

// Apply merges incoming Tick into the existing entry for (exchange, symbol).
//
// Merge strategy:
//
//	Rate, MarkPrice, IndexPrice — overwrite if non-zero in new Tick.
//	Volume24h, OpenIntUSD       — overwrite if non-zero.
//	NextFunding, IntervalH      — overwrite if non-zero.
//	UpdatedAt                   — always overwrite.
//
// This keeps WS ticks (which often carry only rate+mark) merged with REST
// backstop sweeps (which carry volume + open interest) — bug class #7
// from PLAN, where Bybit/OKX/KuCoin pushed deltas that wiped out volume.
func (s *Store) Apply(exchange string, t Tick) {
	if t.Symbol == "" {
		return
	}
	if t.UpdatedAt.IsZero() {
		t.UpdatedAt = time.Now()
	}
	key := exchange + ":" + t.Symbol
	s.mu.Lock()
	defer s.mu.Unlock()
	cur, ok := s.ticks[key]
	if !ok {
		copy := t
		s.ticks[key] = &copy
		return
	}
	if t.Rate != 0 {
		cur.Rate = t.Rate
	}
	if t.MarkPrice != 0 {
		cur.MarkPrice = t.MarkPrice
	}
	if t.IndexPrice != 0 {
		cur.IndexPrice = t.IndexPrice
	}
	if t.Volume24h != 0 {
		cur.Volume24h = t.Volume24h
	}
	if t.OpenIntUSD != 0 {
		cur.OpenIntUSD = t.OpenIntUSD
	}
	if !t.NextFunding.IsZero() {
		cur.NextFunding = t.NextFunding
	}
	if t.IntervalH != 0 {
		cur.IntervalH = t.IntervalH
	}
	cur.UpdatedAt = t.UpdatedAt
}

// SnapshotByExchange returns ticks bucketed per-venue. The slice values
// are copies — caller may mutate without locking.
func (s *Store) SnapshotByExchange() map[string]map[string]Tick {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make(map[string]map[string]Tick, 16)
	for key, t := range s.ticks {
		idx := -1
		for i, c := range key {
			if c == ':' {
				idx = i
				break
			}
		}
		if idx <= 0 {
			continue
		}
		ex := key[:idx]
		sym := key[idx+1:]
		bucket, ok := out[ex]
		if !ok {
			bucket = make(map[string]Tick, 32)
			out[ex] = bucket
		}
		bucket[sym] = *t
	}
	return out
}

// Get returns one entry by (exchange, symbol). Zero value + false if absent.
func (s *Store) Get(exchange, symbol string) (Tick, bool) {
	key := exchange + ":" + symbol
	s.mu.RLock()
	defer s.mu.RUnlock()
	t, ok := s.ticks[key]
	if !ok {
		return Tick{}, false
	}
	return *t, true
}
