package binancealpha

import (
	"context"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
)

const tokensRefreshEvery = 5 * time.Minute

type TokenRef struct {
	Chain        string
	Address      string
	Name         string
	Price        float64
	LiquidityUSD float64
	VolumeUSD    float64
}

// Service caches the full Alpha token list in-memory and exposes a
// symbol → refs resolver. Unlike okxdex there is no separate price
// call: price + liquidity ride the token-list payload and refresh
// together every 5min.
type Service struct {
	client *Client

	mu     sync.RWMutex
	tokens map[string][]TokenRef
}

func NewService(client *Client) *Service {
	return &Service{client: client, tokens: map[string][]TokenRef{}}
}

func buildIndex(rows []Token) map[string][]TokenRef {
	out := make(map[string][]TokenRef, len(rows))
	for _, t := range rows {
		sym := strings.ToUpper(strings.TrimSpace(t.Symbol))
		if sym == "" || t.ContractAddress == "" || t.FullyDelisted {
			continue
		}
		px, err := strconv.ParseFloat(t.Price, 64)
		if err != nil || px <= 0 {
			continue
		}
		liq, _ := strconv.ParseFloat(t.Liquidity, 64)
		vol, _ := strconv.ParseFloat(t.Volume24h, 64)
		// Address keeps its original case — Solana/Sui contracts are
		// base58/hex case-sensitive in URLs; the cex_assets matcher
		// compares with EqualFold so case never breaks verification.
		out[sym] = append(out[sym], TokenRef{
			Chain:        canonicalChain(t.ChainName),
			Address:      strings.TrimSpace(t.ContractAddress),
			Name:         t.Name,
			Price:        px,
			LiquidityUSD: liq,
			VolumeUSD:    vol,
		})
	}
	return out
}

func (s *Service) Refresh(ctx context.Context) error {
	rows, err := s.client.FetchTokens(ctx)
	if err != nil {
		return err
	}
	idx := buildIndex(rows)
	s.mu.Lock()
	s.tokens = idx
	s.mu.Unlock()
	log.L().Info().
		Int("rows", len(rows)).
		Int("symbols", len(idx)).
		Msg("binancealpha: token list refreshed")
	return nil
}

// Run performs an immediate refresh (retried every 30s until first
// success), then re-pulls every 5min. On error the previous map keeps
// serving reads.
func (s *Service) Run(ctx context.Context) {
	warmed := false
	interval := 30 * time.Second
	for {
		if err := s.Refresh(ctx); err != nil {
			if ctx.Err() != nil {
				return
			}
			log.L().Warn().Err(err).Msg("binancealpha: refresh failed — keeping previous map")
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

func (s *Service) TokenSymbolCount() int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return len(s.tokens)
}
