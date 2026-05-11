// Package symbols owns the union of "what should each venue's WS subscribe
// to right now". Two input sources, periodically reconciled into a per-
// venue symbol set:
//
//   1. Prewarm — top-N hot symbols from the bootstrap loader (or in
//      future, from the arb compute output). Set with PrewarmSet().
//
//   2. User touches — pairs that someone has actively open in /arb. Set
//      via Touch(), updated on every Redis book:subscribe message.
//      Idle entries (no touch in IdleWindow) are dropped on the next
//      reconciliation tick.
//
// On every reconciliation tick the manager:
//
//   union := prewarm[venue] ∪ {syms with fresh user-touch}
//   if union != current then runner.SetSymbols(union)
//
// Each Runner's SetSymbols handles WS resubscribe internally — the
// manager doesn't care which symbols are added vs removed.
package symbols

import (
	"context"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/bytedance/sonic"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ws"
)

// IdleWindow — user touch is considered alive for this long after the
// last touch. Matches Python's _USER_SUB_TTL_S default (120s).
const IdleWindow = 120 * time.Second

// Manager is the symbol-set authority. One instance per fetcher process.
type Manager struct {
	mu sync.Mutex

	prewarm  map[string]map[string]struct{} // venue -> symbol set
	userSubs map[string]map[string]time.Time // venue -> symbol -> last touch

	obRunners       map[string]*ws.Runner
	fundingRunners  map[string]*funding.Runner
	tickRunners     map[string]*ticks.Runner

	current map[string]map[string]struct{} // venue -> last-applied set
}

func New() *Manager {
	return &Manager{
		prewarm:        make(map[string]map[string]struct{}),
		userSubs:       make(map[string]map[string]time.Time),
		obRunners:      make(map[string]*ws.Runner),
		fundingRunners: make(map[string]*funding.Runner),
		tickRunners:    make(map[string]*ticks.Runner),
		current:        make(map[string]map[string]struct{}),
	}
}

// RegisterOrderbook attaches an orderbook runner under its venue name
// (the same string used in book:subscribe messages from web).
func (m *Manager) RegisterOrderbook(venue string, r *ws.Runner) {
	m.mu.Lock()
	m.obRunners[venue] = r
	m.mu.Unlock()
}

// RegisterFunding attaches a funding runner. Funding runners share the
// venue keyspace with orderbook (no "_funding" suffix).
func (m *Manager) RegisterFunding(venue string, r *funding.Runner) {
	m.mu.Lock()
	m.fundingRunners[venue] = r
	m.mu.Unlock()
}

// RegisterTicks attaches a trade-stream runner. Shares the venue
// keyspace with orderbook + funding — same symbol set is applied to all
// three runners on every reconcile tick.
func (m *Manager) RegisterTicks(venue string, r *ticks.Runner) {
	m.mu.Lock()
	m.tickRunners[venue] = r
	m.mu.Unlock()
}

// PrewarmSet replaces the prewarm set for one venue. Caller is the
// bootstrap loader (Phase 4 wiring) — no per-venue arb compute yet.
func (m *Manager) PrewarmSet(venue string, syms []string) {
	m.mu.Lock()
	bucket := make(map[string]struct{}, len(syms))
	for _, s := range syms {
		if s != "" {
			bucket[s] = struct{}{}
		}
	}
	m.prewarm[venue] = bucket
	m.mu.Unlock()
}

// PrewarmAll applies the same symbol list to every registered venue.
// Used at startup before per-venue arb data is available.
func (m *Manager) PrewarmAll(syms []string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	bucket := make(map[string]struct{}, len(syms))
	for _, s := range syms {
		if s != "" {
			bucket[s] = struct{}{}
		}
	}
	for venue := range m.obRunners {
		m.prewarm[venue] = copySet(bucket)
	}
	for venue := range m.fundingRunners {
		m.prewarm[venue] = copySet(bucket)
	}
	for venue := range m.tickRunners {
		m.prewarm[venue] = copySet(bucket)
	}
}

// PrewarmFromArbFiles reads arbitrage.json / spot_arbitrage.json /
// dex_arbitrage.json and applies a per-venue prewarm set: each venue
// gets exactly the symbols that appear as one of its legs in the
// top-N arb opportunities. The bootstrap volume-rank fallback union
// is added so we don't lose major pairs (BTC, ETH, …) on any venue
// just because they missed an arb cycle.
//
// Effect on the screener: by the time a user opens /screener every
// (long_ex, short_ex) pair from arbitrage.json has its books
// subscribed, so /api/screener/in-out resolves to real numbers on
// the very first call.
//
// Symbol-set churn between cycles is naturally small — the top-N
// changes one or two pairs per refresh, not the whole set — so
// the SetSymbols delta-subscribe doesn't overwhelm rate-limited
// adapters the way the per-row Touch flood did in Phase B v1.
func (m *Manager) PrewarmFromArbFiles(cacheDir string, fallback []string) {
	type futOpp struct {
		Symbol         string `json:"symbol"`
		LongExchange   string `json:"long_exchange"`
		ShortExchange  string `json:"short_exchange"`
	}
	type spotOpp struct {
		Symbol        string `json:"symbol"`
		SpotExchange  string `json:"spot_exchange"`
		ShortExchange string `json:"short_exchange"`
	}
	type dexOpp struct {
		Symbol        string `json:"symbol"`
		ShortExchange string `json:"short_exchange"`
	}

	tryRead := func(path string, into any) {
		data, err := os.ReadFile(path)
		if err != nil {
			return
		}
		_ = sonic.Unmarshal(data, into)
	}

	// venue -> set(symbol)
	per := make(map[string]map[string]struct{}, 32)
	add := func(venue, sym string) {
		if venue == "" || sym == "" {
			return
		}
		bucket, ok := per[venue]
		if !ok {
			bucket = make(map[string]struct{}, 64)
			per[venue] = bucket
		}
		bucket[sym] = struct{}{}
	}

	{
		var doc struct{ Opps []futOpp `json:"opportunities"` }
		tryRead(filepath.Join(cacheDir, "arbitrage.json"), &doc)
		for _, o := range doc.Opps {
			add(o.LongExchange, o.Symbol)
			add(o.ShortExchange, o.Symbol)
		}
	}
	{
		var doc struct{ Opps []spotOpp `json:"opportunities"` }
		tryRead(filepath.Join(cacheDir, "spot_arbitrage.json"), &doc)
		for _, o := range doc.Opps {
			if o.SpotExchange != "" {
				add(o.SpotExchange+"_spot", o.Symbol)
			}
			add(o.ShortExchange, o.Symbol)
		}
	}
	{
		var doc struct{ Opps []dexOpp `json:"opportunities"` }
		tryRead(filepath.Join(cacheDir, "dex_arbitrage.json"), &doc)
		for _, o := range doc.Opps {
			add(o.ShortExchange, o.Symbol)
		}
	}
	// Funding-feed bridge — every symbol with a live funding tick gets
	// its orderbook prewarmed on that venue. Otherwise users opening /arb
	// on a mid-cap symbol (LAB, etc.) see empty books because Manager
	// only subscribed via arbitrage.json's top-1000. Cap per venue so
	// we don't pile up symbols beyond what rate limits allow.
	{
		type fundRow struct {
			Exchange  string  `json:"exchange"`
			Symbol    string  `json:"symbol"`
			VolumeUsd float64 `json:"volume_usd"`
		}
		var doc struct{ Rows []fundRow `json:"rows"` }
		tryRead(filepath.Join(cacheDir, "funding.json"), &doc)
		byVenue := map[string][]fundRow{}
		for _, r := range doc.Rows {
			if r.Exchange == "" || r.Symbol == "" {
				continue
			}
			byVenue[r.Exchange] = append(byVenue[r.Exchange], r)
		}
		for venue, rows := range byVenue {
			// Sort by volume desc (selection — small N per venue).
			for i := 0; i < len(rows); i++ {
				best := i
				for j := i + 1; j < len(rows); j++ {
					if rows[j].VolumeUsd > rows[best].VolumeUsd {
						best = j
					}
				}
				if best != i {
					rows[i], rows[best] = rows[best], rows[i]
				}
			}
			limit := 250
			if len(rows) < limit {
				limit = len(rows)
			}
			for k := 0; k < limit; k++ {
				add(venue, rows[k].Symbol)
			}
		}
	}

	// Watchlist subscription bridge — Python web role dumps
	// /tmp/avalant_cache/watchlist_subscribe.json every 30s with the
	// union of (sym, long_ex, short_ex) across all users' watchlists.
	// Subscribing those symbols here keeps the orderbook flowing for
	// pairs that have fallen out of the top-N but someone is still
	// watching. Spot watchlist rows put the long_ex on its _spot
	// channel since that's what spot top selection does too.
	{
		type wlPair struct {
			Symbol        string `json:"symbol"`
			LongExchange  string `json:"long_exchange"`
			ShortExchange string `json:"short_exchange"`
		}
		var doc struct{ Pairs []wlPair `json:"pairs"` }
		tryRead(filepath.Join(cacheDir, "watchlist_subscribe.json"), &doc)
		for _, p := range doc.Pairs {
			le := p.LongExchange
			// Match the spot tracker convention: if the long leg is a
			// spot venue (suffix added by callers) leave as-is; if it's
			// a known spot ex name, append `_spot`. Heuristic: the
			// frontend stores `binance` for both spot and perp legs
			// depending on context — when the user is on the spot
			// screener they see e.g. "Binance" too. We can't disambiguate
			// without more state, so we add to BOTH the perp and spot
			// channels (cheap; one extra symbol on the spot WS sub).
			add(le, p.Symbol)
			add(le+"_spot", p.Symbol)
			add(p.ShortExchange, p.Symbol)
		}
	}

	// Apply per venue + add fallback majors.
	m.mu.Lock()
	defer m.mu.Unlock()
	totalSyms := 0
	venues := make(map[string]struct{}, len(m.obRunners)+len(m.fundingRunners)+len(m.tickRunners))
	for v := range m.obRunners {
		venues[v] = struct{}{}
	}
	for v := range m.fundingRunners {
		venues[v] = struct{}{}
	}
	for v := range m.tickRunners {
		venues[v] = struct{}{}
	}
	for v := range venues {
		bucket := per[v]
		if bucket == nil {
			bucket = make(map[string]struct{}, len(fallback))
		}
		for _, s := range fallback {
			if s != "" {
				bucket[s] = struct{}{}
			}
		}
		m.prewarm[v] = bucket
		totalSyms += len(bucket)
	}
	log.L().Info().
		Int("venues", len(venues)).
		Int("arb_venues", len(per)).
		Int("symbols_total", totalSyms).
		Msg("prewarm refreshed from arb files")
}

// Touch records a fresh user-subscribe on (venue, symbol). Called from
// the Redis subscriber on every book:subscribe message.
func (m *Manager) Touch(venue, symbol string) {
	if venue == "" || symbol == "" {
		return
	}
	m.mu.Lock()
	bucket, ok := m.userSubs[venue]
	if !ok {
		bucket = make(map[string]time.Time, 4)
		m.userSubs[venue] = bucket
	}
	bucket[symbol] = time.Now()
	m.mu.Unlock()
}

// Untouch removes the user-sub immediately (called on book:unsubscribe).
func (m *Manager) Untouch(venue, symbol string) {
	m.mu.Lock()
	if bucket, ok := m.userSubs[venue]; ok {
		delete(bucket, symbol)
	}
	m.mu.Unlock()
}

// TouchFromArbFiles reads arb/spot/dex output files and refreshes the
// user-touch set with every (exchange, symbol) pair appearing in any of
// them. The arb compute layer writes top-N opportunities (capped by
// |basis|) so this acts as the screener's "tracked set": as soon as a
// pair enters the top, the orderbook adapter is told to keep its book
// subscribed; when it falls out and the IdleWindow elapses (120 s by
// default), the next reconcile drops it.
//
// Cheap operation — three JSON reads + a fixed number of map writes.
// Errors are silenced (file may not exist yet on a fresh fetcher); we
// just don't refresh the touch set this cycle.
func (m *Manager) TouchFromArbFiles(cacheDir string) {
	type futOpp struct {
		Symbol         string `json:"symbol"`
		LongExchange   string `json:"long_exchange"`
		ShortExchange  string `json:"short_exchange"`
	}
	type spotOpp struct {
		Symbol        string `json:"symbol"`
		SpotExchange  string `json:"spot_exchange"`
		ShortExchange string `json:"short_exchange"`
	}
	type dexOpp struct {
		Symbol        string `json:"symbol"`
		ShortExchange string `json:"short_exchange"`
	}

	tryRead := func(path string, into any) {
		data, err := os.ReadFile(path)
		if err != nil {
			return
		}
		_ = sonic.Unmarshal(data, into)
	}

	// Futures arb — touches both legs.
	{
		var doc struct {
			Opps []futOpp `json:"opportunities"`
		}
		tryRead(filepath.Join(cacheDir, "arbitrage.json"), &doc)
		for _, o := range doc.Opps {
			if o.Symbol == "" {
				continue
			}
			if o.LongExchange != "" {
				m.Touch(o.LongExchange, o.Symbol)
			}
			if o.ShortExchange != "" {
				m.Touch(o.ShortExchange, o.Symbol)
			}
		}
	}
	// Spot/Short — long leg is `<spot_exchange>_spot`.
	{
		var doc struct {
			Opps []spotOpp `json:"opportunities"`
		}
		tryRead(filepath.Join(cacheDir, "spot_arbitrage.json"), &doc)
		for _, o := range doc.Opps {
			if o.Symbol == "" {
				continue
			}
			if o.SpotExchange != "" {
				m.Touch(o.SpotExchange+"_spot", o.Symbol)
			}
			if o.ShortExchange != "" {
				m.Touch(o.ShortExchange, o.Symbol)
			}
		}
	}
	// DEX/Short — DEX side has no orderbook adapter, only the perp leg.
	{
		var doc struct {
			Opps []dexOpp `json:"opportunities"`
		}
		tryRead(filepath.Join(cacheDir, "dex_arbitrage.json"), &doc)
		for _, o := range doc.Opps {
			if o.Symbol == "" || o.ShortExchange == "" {
				continue
			}
			m.Touch(o.ShortExchange, o.Symbol)
		}
	}
	log.L().Debug().Msg("symbol manager: touched arb-file pairs")
}

// Run blocks until ctx is cancelled, reconciling every 5s. Cheap operation
// — just set diff + SetSymbols if changed.
func (m *Manager) Run(ctx context.Context) {
	t := time.NewTicker(5 * time.Second)
	defer t.Stop()
	m.reconcile()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			m.reconcile()
		}
	}
}

func (m *Manager) reconcile() {
	now := time.Now()
	cutoff := now.Add(-IdleWindow)

	m.mu.Lock()
	defer m.mu.Unlock()

	// Build per-venue union of prewarm + fresh user-subs.
	venues := make(map[string]struct{}, len(m.obRunners)+len(m.fundingRunners)+len(m.tickRunners))
	for v := range m.obRunners {
		venues[v] = struct{}{}
	}
	for v := range m.fundingRunners {
		venues[v] = struct{}{}
	}
	for v := range m.tickRunners {
		venues[v] = struct{}{}
	}

	for venue := range venues {
		union := make(map[string]struct{}, 32)
		for s := range m.prewarm[venue] {
			union[s] = struct{}{}
		}
		// Drop expired user-subs while we're here.
		if userBucket, ok := m.userSubs[venue]; ok {
			for s, ts := range userBucket {
				if ts.Before(cutoff) {
					delete(userBucket, s)
					continue
				}
				union[s] = struct{}{}
			}
		}

		// Did the set change since last apply? Use cheap len + sample compare.
		prev := m.current[venue]
		if setsEqual(prev, union) {
			continue
		}
		m.current[venue] = union

		flat := make([]string, 0, len(union))
		for s := range union {
			flat = append(flat, s)
		}

		if r, ok := m.obRunners[venue]; ok {
			r.SetSymbols(flat)
		}
		if r, ok := m.fundingRunners[venue]; ok {
			r.SetSymbols(flat)
		}
		if r, ok := m.tickRunners[venue]; ok {
			r.SetSymbols(flat)
		}
		log.L().Debug().
			Str("venue", venue).
			Int("symbols", len(flat)).
			Int("prewarm", len(m.prewarm[venue])).
			Int("user", len(m.userSubs[venue])).
			Msg("symbol set updated")
	}
}

func copySet(in map[string]struct{}) map[string]struct{} {
	out := make(map[string]struct{}, len(in))
	for k := range in {
		out[k] = struct{}{}
	}
	return out
}

func setsEqual(a, b map[string]struct{}) bool {
	if len(a) != len(b) {
		return false
	}
	for k := range a {
		if _, ok := b[k]; !ok {
			return false
		}
	}
	return true
}
