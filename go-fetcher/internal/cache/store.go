// Package cache holds the shared in-memory orderbook cache and provides the
// atomic file-dump path that Python web roles read.
//
// Concurrency model:
//
//	Many writers (one per WS adapter goroutine) call Store(), which is
//	mutex-protected. One reader (the file-dumper goroutine) calls Snapshot()
//	periodically. Readers are non-blocking — Snapshot() copies pointers, the
//	caller marshals & writes outside the lock.
package cache

import (
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

// Entry is one venue:symbol book + metadata. Mirrors what Python's
// _book_cache holds (data, ts, last_request, source).
type Entry struct {
	Bids    []ws.Level
	Asks    []ws.Level
	UpdatedAt time.Time
	// LastRequestAt — when the orderbook was most recently requested by
	// a web role (touch_user_sub equivalent). Stale entries past
	// IdleTimeout are pruned.
	LastRequestAt time.Time
	// Source: "ws" | "rest" — purely diagnostic, lets the diff script
	// distinguish "WS dropped, REST backstop kicked in" from regressions.
	Source string
}

// Store is the in-process cache. Safe for concurrent Store / Snapshot /
// Touch calls.
type Store struct {
	mu     sync.RWMutex
	books  map[string]*Entry // key = "<exchange>:<symbol>"
}

func New() *Store {
	return &Store{books: make(map[string]*Entry, 1024)}
}

// Store overwrites the entry for (exchange, symbol). Called by WS runner
// on every parsed snapshot and by REST backstops.
func (s *Store) Store(exchange, symbol string, snap ws.Snapshot, source string) {
	key := exchange + ":" + symbol
	s.mu.Lock()
	defer s.mu.Unlock()
	e, ok := s.books[key]
	if !ok {
		e = &Entry{}
		s.books[key] = e
	}
	e.Bids = snap.Bids
	e.Asks = snap.Asks
	e.UpdatedAt = time.Now()
	e.Source = source
	if e.LastRequestAt.IsZero() {
		e.LastRequestAt = e.UpdatedAt
	}
}

// Touch bumps LastRequestAt — called by web roles via Redis pub/sub when
// /arb is open on that pair, so the prewarm pruner doesn't drop it.
func (s *Store) Touch(exchange, symbol string) {
	key := exchange + ":" + symbol
	s.mu.Lock()
	defer s.mu.Unlock()
	if e, ok := s.books[key]; ok {
		e.LastRequestAt = time.Now()
	} else {
		// Pre-create empty entry so the WS subscriber sees the request
		// even before the first frame arrives.
		s.books[key] = &Entry{LastRequestAt: time.Now()}
	}
}

// Get reads one entry. Returns (nil, false) if absent.
func (s *Store) Get(exchange, symbol string) (*Entry, bool) {
	key := exchange + ":" + symbol
	s.mu.RLock()
	defer s.mu.RUnlock()
	e, ok := s.books[key]
	if !ok {
		return nil, false
	}
	// Return a shallow copy — caller must not mutate slices.
	cp := *e
	return &cp, true
}

// Snapshot returns a shallow copy of the whole cache for the file dumper.
// Slices are shared with the live store — safe because writers always
// allocate new slices on update (never append in-place).
func (s *Store) Snapshot() map[string]Entry {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make(map[string]Entry, len(s.books))
	for k, v := range s.books {
		out[k] = *v
	}
	return out
}

// Prune removes entries whose LastRequestAt is older than `idle`. Returns
// removed count. Caller schedules — typically every 60s.
func (s *Store) Prune(idle time.Duration) int {
	cutoff := time.Now().Add(-idle)
	s.mu.Lock()
	defer s.mu.Unlock()
	removed := 0
	for k, e := range s.books {
		if e.LastRequestAt.Before(cutoff) {
			delete(s.books, k)
			removed++
		}
	}
	return removed
}
