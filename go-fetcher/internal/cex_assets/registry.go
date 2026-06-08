package cex_assets

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
)

// AssetAddress is one (chain, contract) entry for a token on one CEX.
type AssetAddress struct {
	Chain   string `json:"chain"`   // DexScreener canonical id ("ethereum", "bsc", ...)
	Address string `json:"address"` // lowercase; "" for L1 natives
}

// VenueAssets is the per-venue map: ticker → list of (chain, address) pairs.
type VenueAssets map[string][]AssetAddress

// Snapshot is the persisted format. One file written per refresh cycle.
type Snapshot struct {
	GeneratedAt int64                  `json:"generated_at"`
	Venues      map[string]VenueAssets `json:"venues"`
}

// MatchResult is what compute consumers receive from MatchByAddress.
type MatchResult struct {
	// Verified is true when (venue, ticker) has an entry whose chain +
	// address exactly equal the DEX side. False means: ticker found but
	// no chain/address match (collision risk), OR ticker not in venue's
	// registry at all (e.g. registry empty, venue's adapter disabled,
	// asset listed but address API unreachable).
	Verified bool
	// MatchChain — populated when Verified=true, gives the canonical
	// chain id where the match occurred. Useful for logging + the UI
	// pill ("matched on Ethereum").
	MatchChain string
	// AddressKnown is true when the registry has ANY chain entry for
	// (venue, ticker). Useful to distinguish "we tried and the addresses
	// don't match" from "we have no data on this venue's listing".
	// Drives the unverified-reason field in dex/spot opp output.
	AddressKnown bool
}

// Registry holds the in-memory address index plus a write-through file
// persistence at cacheDir/cex_assets.json. Safe for concurrent reads;
// writes go through the publish path (Update + persist), which is
// called by the manager once per refresh cycle.
type Registry struct {
	cacheDir string

	mu        sync.RWMutex
	venues    map[string]VenueAssets // venue → ticker → addresses
	updatedAt time.Time
}

// NewRegistry constructs an empty registry rooted at cacheDir. Call
// LoadFromDisk afterwards to populate from the last snapshot (returns
// nil if no file exists — that's normal first run).
func NewRegistry(cacheDir string) *Registry {
	return &Registry{
		cacheDir: cacheDir,
		venues:   make(map[string]VenueAssets, 9),
	}
}

// LoadFromDisk hydrates the registry from cacheDir/cex_assets.json if
// it exists. Persistence policy: even a stale snapshot is far better
// than empty on startup — addresses change roughly once per year per
// chain when projects deploy v2 contracts. fail-soft: missing file or
// parse error returns nil; the manager's refresh cycle will populate
// from APIs.
func (r *Registry) LoadFromDisk() error {
	path := filepath.Join(r.cacheDir, "cex_assets.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		return err
	}
	var snap Snapshot
	if err := json.Unmarshal(raw, &snap); err != nil {
		return err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if snap.Venues != nil {
		r.venues = snap.Venues
	}
	if snap.GeneratedAt > 0 {
		r.updatedAt = time.Unix(snap.GeneratedAt, 0)
	}
	log.L().Info().
		Int("venues", len(r.venues)).
		Int64("generated_at", snap.GeneratedAt).
		Msg("cex_assets loaded from disk")
	return nil
}

// SetVenue atomically replaces the assets for one venue. Called by
// adapters via the manager after a successful refresh. Other venues'
// data is preserved — a single venue failing doesn't wipe the registry.
func (r *Registry) SetVenue(venue string, assets VenueAssets) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.venues[venue] = assets
	r.updatedAt = time.Now()
}

// PersistToDisk writes the current state to cacheDir/cex_assets.json
// atomically (.tmp + rename). Called by the manager at the end of a
// full refresh cycle, not after each individual SetVenue, to keep the
// file consistent with a multi-venue snapshot.
func (r *Registry) PersistToDisk() error {
	r.mu.RLock()
	snap := Snapshot{
		GeneratedAt: time.Now().Unix(),
		Venues:      make(map[string]VenueAssets, len(r.venues)),
	}
	for v, m := range r.venues {
		snap.Venues[v] = m
	}
	r.mu.RUnlock()
	raw, err := json.Marshal(snap)
	if err != nil {
		return err
	}
	path := filepath.Join(r.cacheDir, "cex_assets.json")
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// MatchByAddress is the read-path consumers use. Given a DEX-side
// (chain, address) plus a CEX (venue, ticker), it returns whether the
// venue lists the same token on the same chain at the same address.
//
// Inputs:
//   venue   — CEX venue id ("gate", "kucoin", "bitget", ...)
//   ticker  — CEX ticker, case-insensitive
//   dexChain — DexScreener canonical chain id ("ethereum", "bsc", ...)
//   dexAddress — DEX-side contract, lowercase
//
// Match policy:
//   - Verified=true ONLY when the venue has a (chain, address) entry
//     whose chain equals dexChain AND address equals dexAddress.
//   - "" chain on either side (native L1) NEVER matches — those tokens
//     aren't DexScreener-mappable anyway.
//   - AddressKnown=true when the venue has any entry for the ticker,
//     even if no chain/address matches. Differentiates "data exists,
//     no match" from "no data".
func (r *Registry) MatchByAddress(venue, ticker, dexChain, dexAddress string) MatchResult {
	if venue == "" || ticker == "" || dexChain == "" || dexAddress == "" {
		return MatchResult{}
	}
	t := strings.ToUpper(strings.TrimSpace(ticker))
	a := strings.ToLower(strings.TrimSpace(dexAddress))
	chain := strings.ToLower(strings.TrimSpace(dexChain))

	r.mu.RLock()
	defer r.mu.RUnlock()
	venueMap, ok := r.venues[strings.ToLower(venue)]
	if !ok {
		return MatchResult{}
	}
	entries, ok := venueMap[t]
	if !ok || len(entries) == 0 {
		return MatchResult{}
	}
	res := MatchResult{AddressKnown: true}
	for _, e := range entries {
		if e.Chain == "" || e.Address == "" {
			continue
		}
		if strings.EqualFold(e.Chain, chain) && strings.EqualFold(e.Address, a) {
			return MatchResult{Verified: true, MatchChain: e.Chain, AddressKnown: true}
		}
	}
	return res
}

// VenueCount returns the number of venues currently in the registry.
// Used by the manager to log + by health endpoints.
func (r *Registry) VenueCount() int {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return len(r.venues)
}

// LastUpdated returns the last SetVenue / LoadFromDisk timestamp.
func (r *Registry) LastUpdated() time.Time {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.updatedAt
}

// SizeByVenue returns the per-venue ticker count. Used in startup log
// + ops health checks ("kucoin has 875 tickers, bitget has 920" etc.)
// to spot adapter regressions quickly.
func (r *Registry) SizeByVenue() map[string]int {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make(map[string]int, len(r.venues))
	for v, m := range r.venues {
		out[v] = len(m)
	}
	return out
}
