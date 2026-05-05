// inout.go — event-driven in/out patcher.
//
// When the orderbook store receives a new snapshot for (exchange, symbol),
// OnBookUpdate immediately recomputes in_pct / out_pct for every arb pair
// that involves that exchange×symbol and pushes a targeted WS diff to the
// long-short hub — bypassing the 500ms arb-compute cycle.
//
// Rollback: env AVALANT_INOUT_REALTIME=0 skips creating the patcher.
// The 500ms arb-compute + 250ms LongShort broadcaster continue as before.
//
// Pair index refresh: reads all three arb files every inoutIndexInterval.
// Lags by at most one interval for brand-new pairs; irrelevant in practice
// because the index refreshes faster than the arb compute that produces them.
package wsbroadcast

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/arb"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
)

const inoutIndexInterval = 500 * time.Millisecond

// InOutPatcher maintains a reverse index (exchange:symbol → []pairKey) built
// from the three arb cache files, and pushes targeted in/out diffs to the
// long-short Hub whenever an orderbook update lands.
type InOutPatcher struct {
	books    *cache.Store
	hub      *Hub
	cacheDir string

	mu       sync.RWMutex
	affected map[string][]string      // "ex:sym" → []pairKey
	pairs    map[string]map[string]any // pairKey → opp snapshot

	lastMu  sync.Mutex
	lastVal map[string][2]*float64 // last pushed in/out per pairKey
}

func NewInOutPatcher(books *cache.Store, hub *Hub, cacheDir string) *InOutPatcher {
	p := &InOutPatcher{
		books:    books,
		hub:      hub,
		cacheDir: cacheDir,
		affected: make(map[string][]string),
		pairs:    make(map[string]map[string]any),
		lastVal:  make(map[string][2]*float64),
	}
	return p
}

// Run refreshes the reverse index at inoutIndexInterval. Call in its own
// goroutine; blocks until ctx is cancelled.
func (p *InOutPatcher) Run(ctx context.Context) {
	p.refreshIndex()
	t := time.NewTicker(inoutIndexInterval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			p.refreshIndex()
		}
	}
}

// OnBookUpdate is the onUpdate hook for cache.Store. Called from the WS
// adapter goroutine on every orderbook snapshot; must be fast (no I/O).
func (p *InOutPatcher) OnBookUpdate(exchange, symbol string) {
	if p.hub.Count() == 0 {
		return
	}
	affKey := exchange + ":" + symbol

	p.mu.RLock()
	pairKeys := p.affected[affKey]
	if len(pairKeys) == 0 {
		p.mu.RUnlock()
		return
	}
	type snap struct {
		key  string
		opp  map[string]any
		mode string
	}
	snaps := make([]snap, 0, len(pairKeys))
	for _, k := range pairKeys {
		if opp, ok := p.pairs[k]; ok {
			mode, _ := opp["_mode"].(string)
			snaps = append(snaps, snap{key: k, opp: opp, mode: mode})
		}
	}
	p.mu.RUnlock()

	if len(snaps) == 0 {
		return
	}

	updated := make([]any, 0, len(snaps))
	p.lastMu.Lock()
	for _, s := range snaps {
		var inPct, outPct *float64
		switch s.mode {
		case "dex":
			shortEx, _ := s.opp["short_exchange"].(string)
			sym, _ := s.opp["symbol"].(string)
			dexPrice, _ := s.opp["dex_price"].(float64)
			inPct, outPct = arb.ComputeInOutDex(p.books, shortEx, sym, dexPrice)
		case "spot":
			spotEx, _ := s.opp["spot_exchange"].(string)
			shortEx, _ := s.opp["short_exchange"].(string)
			sym, _ := s.opp["symbol"].(string)
			inPct, outPct = arb.ComputeInOutPair(p.books, spotEx+"_spot", shortEx, sym)
		default: // futures
			longEx, _ := s.opp["long_exchange"].(string)
			shortEx, _ := s.opp["short_exchange"].(string)
			sym, _ := s.opp["symbol"].(string)
			inPct, outPct = arb.ComputeInOutPair(p.books, longEx, shortEx, sym)
		}

		prev := p.lastVal[s.key]
		if !inoutDiffers(prev[0], inPct) && !inoutDiffers(prev[1], outPct) {
			continue
		}
		p.lastVal[s.key] = [2]*float64{inPct, outPct}

		row := inoutCloneOpp(s.opp)
		delete(row, "_mode")
		row["in_pct"] = inPct
		row["out_pct"] = outPct
		updated = append(updated, row)
	}
	p.lastMu.Unlock()

	if len(updated) == 0 {
		return
	}

	payload := map[string]any{
		"type":    "diff",
		"ts":      time.Now().Unix(),
		"updated": updated,
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return
	}
	p.hub.Broadcast(body)
	log.L().Trace().Int("pairs", len(updated)).Str("ex", exchange).Str("sym", symbol).Msg("inout realtime push")
}

// refreshIndex rebuilds the reverse index from all three arb cache files.
func (p *InOutPatcher) refreshIndex() {
	newAff := make(map[string][]string, 2048)
	newPairs := make(map[string]map[string]any, 1024)

	// Futures: long_exchange and short_exchange both have OBs.
	p.loadBothSides(
		filepath.Join(p.cacheDir, "arbitrage.json"), "futures",
		"long_exchange", "short_exchange",
		newAff, newPairs,
	)
	// Spot: spot OB key is "<spotEx>_spot", perp short is bare name.
	p.loadBothSides(
		filepath.Join(p.cacheDir, "spot_arbitrage.json"), "spot",
		"spot_exchange", "short_exchange",
		newAff, newPairs,
	)
	// DEX: only the short (perp) side has an OB.
	p.loadShortOnly(
		filepath.Join(p.cacheDir, "dex_arbitrage.json"), "dex",
		newAff, newPairs,
	)

	p.mu.Lock()
	p.affected = newAff
	p.pairs = newPairs
	p.mu.Unlock()
}

func (p *InOutPatcher) loadBothSides(
	path, mode, longField, shortField string,
	aff map[string][]string, pairs map[string]map[string]any,
) {
	opps := readOpps(path)
	for _, opp := range opps {
		sym, _ := opp["symbol"].(string)
		longEx, _ := opp[longField].(string)
		shortEx, _ := opp[shortField].(string)
		if sym == "" || longEx == "" || shortEx == "" {
			continue
		}
		k := sym + "|" + longEx + "|" + shortEx
		cp := inoutCloneOpp(opp)
		cp["_mode"] = mode
		pairs[k] = cp

		// Short side — always has an OB under the bare exchange name.
		aff[shortEx+":"+sym] = inoutAppendUniq(aff[shortEx+":"+sym], k)

		// Long side — spot uses "<spotEx>_spot" as the OB store key.
		var longKey string
		if mode == "spot" {
			longKey = longEx + "_spot:" + sym
		} else {
			longKey = longEx + ":" + sym
		}
		aff[longKey] = inoutAppendUniq(aff[longKey], k)
	}
}

func (p *InOutPatcher) loadShortOnly(
	path, mode string,
	aff map[string][]string, pairs map[string]map[string]any,
) {
	opps := readOpps(path)
	for _, opp := range opps {
		sym, _ := opp["symbol"].(string)
		dexName, _ := opp["dex_name"].(string)
		shortEx, _ := opp["short_exchange"].(string)
		if sym == "" || dexName == "" || shortEx == "" {
			continue
		}
		k := sym + "|" + dexName + "|" + shortEx
		cp := inoutCloneOpp(opp)
		cp["_mode"] = mode
		pairs[k] = cp

		// Only the perp short leg has an orderbook.
		aff[shortEx+":"+sym] = inoutAppendUniq(aff[shortEx+":"+sym], k)
	}
}

func readOpps(path string) []map[string]any {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var doc map[string]any
	if json.Unmarshal(raw, &doc) != nil {
		return nil
	}
	opps, _ := doc["opportunities"].([]any)
	out := make([]map[string]any, 0, len(opps))
	for _, o := range opps {
		if m, ok := o.(map[string]any); ok {
			out = append(out, m)
		}
	}
	return out
}

func inoutCloneOpp(opp map[string]any) map[string]any {
	cp := make(map[string]any, len(opp)+1)
	for k, v := range opp {
		cp[k] = v
	}
	return cp
}

func inoutAppendUniq(s []string, v string) []string {
	for _, x := range s {
		if x == v {
			return s
		}
	}
	return append(s, v)
}

func inoutDiffers(a, b *float64) bool {
	if (a == nil) != (b == nil) {
		return true
	}
	if a == nil {
		return false
	}
	return *a != *b
}
