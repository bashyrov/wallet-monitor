package okxdex

import (
	"context"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cex_assets"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
)

const tokensRefreshEvery = 5 * time.Minute

type TokenRef struct {
	ChainIndex string
	Address    string
	Decimals   int
	ChainName  string
}

// canonicalNameToChainIndex — inverse of arb.okxChainCanonical, kept in
// sync manually (both maps must add a new chain in the same commit).
// Only chains that OKX /dex/market/price supports get an entry; a
// registry ticker on an unsupported chain (e.g. sui/aptos/ton — cex_assets
// lists them but OKX prices don't cover them) is skipped without error.
var canonicalNameToChainIndex = map[string]string{
	"ethereum":  "1",
	"bsc":       "56",
	"polygon":   "137",
	"arbitrum":  "42161",
	"optimism":  "10",
	"base":      "8453",
	"avalanche": "43114",
	"fantom":    "250",
	"zksync":    "324",
	"linea":     "59144",
	"scroll":    "534352",
	"mantle":    "5000",
	"blast":     "81457",
	"solana":    "501",
	"tron":      "195",
}

// Service owns the symbol → []TokenRef resolver map. Discovery source
// is the cex_assets.Registry (symbol→chain→contract from 5+ CEX asset
// APIs, 24h refresh). Price source is OKX /dex/market/price by
// contract address — accepts any AMM-indexed token, no whitelist.
type Service struct {
	client *Client
	reg    *cex_assets.Registry

	mu         sync.RWMutex
	chainNames map[string]string     // chainIndex → display name (from OKX supported/chain)
	tokens     map[string][]TokenRef // UPPER(symbol) → refs (dedup'd across venues)
}

func NewService(client *Client, reg *cex_assets.Registry) *Service {
	return &Service{
		client:     client,
		reg:        reg,
		chainNames: map[string]string{},
		tokens:     map[string][]TokenRef{},
	}
}

func (s *Service) Configured() bool { return s.client.Configured() && s.reg != nil }

// Refresh rebuilds the symbol → refs index from the cex_assets registry.
// Cheap (in-memory iteration), safe to run every 5min so a fresh CEX
// asset refresh propagates without a fetcher restart. On the first call
// also pulls the OKX chain-list once for display names — a failed pull
// leaves the chainName column empty, which is harmless.
func (s *Service) Refresh(ctx context.Context) error {
	if s.reg == nil {
		return ErrNotConfigured
	}
	// One-shot chainName fetch — nice-to-have for the output display.
	// Missing it doesn't stop us (row.ChainName just falls back to "").
	s.mu.RLock()
	haveChains := len(s.chainNames) > 0
	s.mu.RUnlock()
	if !haveChains && s.client.Configured() {
		if names, err := s.client.FetchChains(ctx); err == nil {
			s.mu.Lock()
			s.chainNames = names
			s.mu.Unlock()
		} else {
			log.L().Warn().Err(err).Msg("okxdex.Tokens: chain-list fetch failed — display names will be empty")
		}
	}

	venues := s.reg.All()
	// Dedupe by (chain, address); one symbol may appear across venues
	// with the same contract — one entry is enough.
	type key struct{ chain, addr string }
	seen := make(map[string]map[key]struct{}, 8192)
	newTokens := make(map[string][]TokenRef, 8192)

	var totalRefs, skipUnknownChain, skipEmpty int
	s.mu.RLock()
	chainNamesCopy := s.chainNames
	s.mu.RUnlock()

	for _, venueMap := range venues {
		for ticker, addrs := range venueMap {
			sym := strings.ToUpper(strings.TrimSpace(ticker))
			if sym == "" {
				continue
			}
			for _, a := range addrs {
				if a.Chain == "" || a.Address == "" {
					skipEmpty++
					continue
				}
				chainIdx, ok := canonicalNameToChainIndex[strings.ToLower(a.Chain)]
				if !ok {
					skipUnknownChain++
					continue
				}
				addrLower := strings.ToLower(a.Address)
				k := key{chain: chainIdx, addr: addrLower}
				if seen[sym] == nil {
					seen[sym] = make(map[key]struct{}, 4)
				}
				if _, dup := seen[sym][k]; dup {
					continue
				}
				seen[sym][k] = struct{}{}
				newTokens[sym] = append(newTokens[sym], TokenRef{
					ChainIndex: chainIdx,
					Address:    addrLower,
					ChainName:  chainNamesCopy[chainIdx],
				})
				totalRefs++
			}
		}
	}

	s.mu.Lock()
	s.tokens = newTokens
	s.mu.Unlock()

	log.L().Info().
		Int("symbols", len(newTokens)).
		Int("refs", totalRefs).
		Int("venues", len(venues)).
		Int("skip_unknown_chain", skipUnknownChain).
		Msg("okxdex.Tokens: rebuilt index from cex_assets registry")
	return nil
}

// Run performs an immediate refresh (retried every 30s until first
// success — registry may be empty for ~30s after a cold boot before
// cex_assets.Manager's first refresh lands), then rebuilds every 5min
// so newly-added CEX tickers propagate without a restart.
func (s *Service) Run(ctx context.Context) {
	warmed := false
	interval := 30 * time.Second
	for {
		if err := s.Refresh(ctx); err != nil {
			if ctx.Err() != nil {
				return
			}
			log.L().Warn().Err(err).Msg("okxdex.Tokens: refresh failed — keeping previous map")
		} else {
			s.mu.RLock()
			n := len(s.tokens)
			s.mu.RUnlock()
			if n > 0 {
				warmed = true
			}
		}
		if warmed {
			interval = tokensRefreshEvery
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(interval):
		}
	}
}

// LookupBySymbol returns all chain placements for a symbol (case-
// insensitive). The returned slice is shared — callers must not mutate.
func (s *Service) LookupBySymbol(sym string) []TokenRef {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.tokens[strings.ToUpper(sym)]
}

// FetchPrices proxies to the underlying client.
func (s *Service) FetchPrices(ctx context.Context, refs []PriceReq) (map[string]float64, error) {
	return s.client.FetchPrices(ctx, refs)
}

func (s *Service) TokenSymbolCount() int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return len(s.tokens)
}
