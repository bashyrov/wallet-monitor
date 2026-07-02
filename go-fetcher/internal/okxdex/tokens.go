package okxdex

import (
	"context"
	"fmt"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
)

const tokensRefreshEvery = 1 * time.Hour

type TokenRef struct {
	ChainIndex string
	Address    string
	Decimals   int
	ChainName  string
}

type tokenRow struct {
	Decimals             string `json:"decimals"`
	TokenContractAddress string `json:"tokenContractAddress"`
	TokenSymbol          string `json:"tokenSymbol"`
}

// Service owns the symbol → []TokenRef resolver map, refreshed hourly
// from the per-chain all-tokens sweep. In-memory only.
type Service struct {
	client *Client

	mu     sync.RWMutex
	chains map[string]string     // chainIndex → chainName
	tokens map[string][]TokenRef // UPPER(symbol) → refs (one per chain)
}

func NewService(client *Client) *Service {
	return &Service{
		client: client,
		chains: map[string]string{},
		tokens: map[string][]TokenRef{},
	}
}

func (s *Service) Configured() bool { return s.client.Configured() }

// Refresh sweeps the chain list + all-tokens per chain and swaps the
// resolver map atomically. A partially-failed sweep still installs what
// it got (per-chain failures are logged and skipped); a failed chain
// list keeps the previous map.
func (s *Service) Refresh(ctx context.Context) error {
	if !s.client.Configured() {
		return ErrNotConfigured
	}
	chains, err := s.client.FetchChains(ctx)
	if err != nil {
		return fmt.Errorf("chain list: %w", err)
	}
	newTokens := make(map[string][]TokenRef, 8192)
	var total, okChains int
	first := true
	for idx, name := range chains {
		// Pace the sweep unconditionally (error paths included) — the
		// all-tokens endpoint is limited to ~1 req/s and 429s cascade
		// otherwise.
		if !first {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(1100 * time.Millisecond):
			}
		}
		first = false
		var rows []tokenRow
		path := "/api/v6/dex/aggregator/all-tokens?chainIndex=" + url.QueryEscape(idx)
		if err := s.client.do(ctx, "GET", path, nil, &rows); err != nil {
			log.L().Warn().Err(err).Str("chain", name).Str("chainIndex", idx).Msg("okxdex.Tokens: chain sweep failed — skipping")
			continue
		}
		okChains++
		for _, r := range rows {
			sym := strings.ToUpper(strings.TrimSpace(r.TokenSymbol))
			if sym == "" || r.TokenContractAddress == "" {
				continue
			}
			dec, _ := strconv.Atoi(r.Decimals)
			newTokens[sym] = append(newTokens[sym], TokenRef{
				ChainIndex: idx,
				Address:    strings.ToLower(r.TokenContractAddress),
				Decimals:   dec,
				ChainName:  name,
			})
			total++
		}
	}
	if okChains == 0 {
		return fmt.Errorf("all %d chain sweeps failed", len(chains))
	}
	s.mu.Lock()
	s.chains = chains
	s.tokens = newTokens
	s.mu.Unlock()
	log.L().Info().Int("tokens", total).Int("chains", okChains).Msgf("okxdex.Tokens: loaded %d tokens across %d chains", total, okChains)
	return nil
}

// Run owns the refresh lifecycle: an immediate initial sweep, retried
// every 2 minutes until it first succeeds (the full paced sweep takes
// 2-4 min, so this can't be a blocking startup step), then hourly.
func (s *Service) Run(ctx context.Context) {
	interval := 2 * time.Minute
	warmed := false
	for {
		if err := s.Refresh(ctx); err != nil {
			if ctx.Err() != nil {
				return
			}
			log.L().Warn().Err(err).Msg("okxdex.Tokens: refresh failed — keeping previous map")
		} else {
			warmed = true
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
