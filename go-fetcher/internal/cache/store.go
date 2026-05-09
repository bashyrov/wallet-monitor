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
	mu    sync.RWMutex
	books map[string]*Entry // key = "<exchange>:<symbol>"

	// versions holds a per-venue update counter, bumped on every Store()
	// call for that exchange. The Dumper uses this to skip rewriting
	// books.<ex>.json files for venues that haven't changed since the
	// last tick — a 100ms cadence × 12 venues was burning ~70% I/O on
	// idle venues that already had fresh files on disk.
	versions map[string]uint64

	// Optional hook fired on every Store() call. Used by the Phase 6
	// rollout to mirror updates into Redis (`ob:<ex>:<sym>` keys) so
	// Python web's existing read path picks up Go-fetched data without
	// any code change on the web side.
	onUpdate func(exchange, symbol string, bids, asks []ws.Level)
}

func New() *Store {
	return &Store{
		books:    make(map[string]*Entry, 1024),
		versions: make(map[string]uint64, 32),
	}
}

// SetOnUpdate registers a hook called on every Store() with the parsed
// snapshot. Set once at wiring time; nil hook is a no-op.
func (s *Store) SetOnUpdate(fn func(exchange, symbol string, bids, asks []ws.Level)) {
	s.mu.Lock()
	s.onUpdate = fn
	s.mu.Unlock()
}

// Store overwrites the entry for (exchange, symbol). Called by WS runner
// on every parsed snapshot and by REST backstops.
func (s *Store) Store(exchange, symbol string, snap ws.Snapshot, source string) {
	key := exchange + ":" + symbol
	s.mu.Lock()
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
	s.versions[exchange]++
	hook := s.onUpdate
	s.mu.Unlock()

	if hook != nil {
		hook(exchange, symbol, snap.Bids, snap.Asks)
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

// Versions returns a copy of the per-venue update counter map. Callers
// (the file dumper) compare against their own last-seen value to decide
// whether a venue's file needs rewriting.
func (s *Store) Versions() map[string]uint64 {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make(map[string]uint64, len(s.versions))
	for k, v := range s.versions {
		out[k] = v
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

// EvictStale removes entries whose UpdatedAt is older than `staleAfter`
// regardless of LastRequestAt. Catches the case where a WS adapter
// remains "subscribed" to a contract that the venue has stopped pushing
// deltas for (delisted / halted) — Prune wouldn't catch it because the
// per-process userSubs map keeps refreshing LastRequestAt.
//
// Returns count of entries dropped. Caller decides cadence; we use 60s.
func (s *Store) EvictStale(staleAfter time.Duration) int {
	cutoff := time.Now().Add(-staleAfter)
	s.mu.Lock()
	defer s.mu.Unlock()
	removed := 0
	for k, e := range s.books {
		if e.UpdatedAt.Before(cutoff) {
			delete(s.books, k)
			removed++
		}
	}
	return removed
}
