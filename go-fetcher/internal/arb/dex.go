package arb

import (
	"context"
	"fmt"
	"math"
	"net/http"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/bytedance/sonic"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
)

// dexInfo is the result of a DexScreener pool lookup.
type dexInfo struct {
	Symbol       string
	Chain        string
	Dex          string
	Price        float64
	LiquidityUSD float64
	VolumeUSD    float64
	BaseAddress  string
	PairURL      string
}

// DEXCompute is the Go port of dex_arbitrage_service.py. Refreshes a
// CoinGecko symbol→contract cache hourly, fetches DexScreener pool data
// per contract for the top-N mcap symbols intersecting the funding
// store, builds dex-short opportunities, writes dex_arbitrage.json.
type DEXCompute struct {
	store    *funding.Store
	cacheDir string
	interval time.Duration

	mu        sync.Mutex
	cgCache   map[string]cgEntry // symbol → entry (top-mcap winner)
	cgUpdated time.Time

	firstSeen map[dexKey]time.Time
	lastSeen  map[dexKey]time.Time
}

type cgEntry struct {
	mcapRank int
	chain    string // dexscreener chain id
	contract string // base token contract
}

type dexKey struct {
	symbol string
	perpEx string
}

const (
	cgTTL              = 1 * time.Hour
	dexOppMinLifetime  = 25 * time.Second
	dexOppPurgeAfter   = 5 * time.Minute
	maxBasisPct        = 100.0
	maxMcapRank        = 5_000
	symbolBatchLimit   = 900
	dexFeeRoundtripPct = 0.6 + 0.2 // 0.3%×2 swap + 0.2% slippage
	minDEXLiqUSD       = 5_000.0
	minDEXVolUSD       = 1_000.0
	dexConsensusMaxDev = 0.015 // 1.5% — single-pool jitter guard
)

// CoinGecko slug → DexScreener chainId.
var cgToDS = map[string]string{
	"ethereum":              "ethereum",
	"solana":                "solana",
	"binance-smart-chain":   "bsc",
	"polygon-pos":           "polygon",
	"arbitrum-one":           "arbitrum",
	"optimistic-ethereum":   "optimism",
	"base":                  "base",
	"avalanche":             "avalanche",
	"fantom":                "fantom",
	"linea":                 "linea",
	"scroll":                "scroll",
	"mantle":                "mantle",
	"blast":                 "blast",
	"zksync":                "zksync",
	"sui":                   "sui",
	"tron":                  "tron",
	"ton":                   "ton",
	"aptos":                 "aptos",
}

var chainPreference = []string{
	"ethereum", "solana", "base", "arbitrum", "bsc", "polygon",
	"optimism", "avalanche", "blast", "linea", "scroll", "mantle", "sui", "ton",
}

var acceptedQuotes = map[string]struct{}{
	"USDT": {}, "USDC": {}, "WETH": {}, "WBTC": {}, "WBNB": {}, "WSOL": {},
	"SOL": {}, "ETH": {}, "BNB": {}, "MATIC": {}, "WMATIC": {},
}

func NewDEXCompute(store *funding.Store, cacheDir string, interval time.Duration) *DEXCompute {
	return &DEXCompute{
		store:     store,
		cacheDir:  cacheDir,
		interval:  interval,
		cgCache:   make(map[string]cgEntry),
		firstSeen: make(map[dexKey]time.Time),
		lastSeen:  make(map[dexKey]time.Time),
	}
}

func (c *DEXCompute) Run(ctx context.Context) error {
	t := time.NewTicker(c.interval)
	defer t.Stop()
	// Warm CG cache + first compute, after a delay so funding store is ready.
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-time.After(10 * time.Second):
	}
	c.tick(ctx)
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-t.C:
			c.tick(ctx)
		}
	}
}

func (c *DEXCompute) tick(ctx context.Context) {
	c.refreshCG(ctx)
	c.mu.Lock()
	cgSize := len(c.cgCache)
	c.mu.Unlock()
	if cgSize == 0 {
		// No CG cache yet — write empty file so consumers see structure.
		c.writeEmpty()
		return
	}

	// Build perp map from funding store (one entry per symbol×exchange).
	perpMap := make(map[string]map[string]funding.Tick, 1024)
	for ex, bucket := range c.store.SnapshotByExchange() {
		if ex == "lighter" {
			continue
		}
		for sym, t := range bucket {
			if t.MarkPrice <= 0 || t.IntervalH <= 0 || t.Rate == 0 {
				continue
			}
			if t.Volume24h > 0 && t.Volume24h < minVolumeUSD {
				continue
			}
			pBucket, ok := perpMap[sym]
			if !ok {
				pBucket = make(map[string]funding.Tick, 4)
				perpMap[sym] = pBucket
			}
			pBucket[ex] = t
		}
	}
	if len(perpMap) == 0 {
		c.writeEmpty()
		return
	}

	// Pick mappable symbols, sort by mcap rank, cap to symbolBatchLimit.
	type rankedSym struct {
		sym  string
		rank int
		entry cgEntry
	}
	candidates := make([]rankedSym, 0, len(perpMap))
	c.mu.Lock()
	for sym := range perpMap {
		entry, ok := c.cgCache[sym]
		if !ok || entry.mcapRank >= maxMcapRank {
			continue
		}
		candidates = append(candidates, rankedSym{sym: sym, rank: entry.mcapRank, entry: entry})
	}
	c.mu.Unlock()
	sort.Slice(candidates, func(i, j int) bool { return candidates[i].rank < candidates[j].rank })
	if len(candidates) > symbolBatchLimit {
		candidates = candidates[:symbolBatchLimit]
	}

	// Parallel DexScreener fetches.
	type fetched struct {
		sym  string
		info *dexInfo
	}
	results := make(chan fetched, len(candidates))
	const workers = 12
	sem := make(chan struct{}, workers)
	var wg sync.WaitGroup
	dsCtx, cancel := context.WithTimeout(ctx, 90*time.Second)
	defer cancel()

	// Debug counters — surfaced in the cycle-end log so we can see WHERE
	// rows are being dropped (404 vs no-pools vs filter vs consensus).
	var (
		cntHTTPErr    int64
		cntHTTPOK     int64
		cntNoPairs    int64
		cntChainMiss  int64
		cntQuoteFilt  int64
		cntLiqFloor   int64
		cntConsensFL  int64
		cntHits       int64
	)

	for _, cand := range candidates {
		cand := cand
		wg.Add(1)
		sem <- struct{}{}
		go func() {
			defer wg.Done()
			defer func() { <-sem }()
			info, reason := fetchDexPoolDbg(dsCtx, cand.entry.chain, cand.entry.contract)
			switch reason {
			case "http_err":  atomic.AddInt64(&cntHTTPErr, 1)
			case "ok":        atomic.AddInt64(&cntHTTPOK, 1); atomic.AddInt64(&cntHits, 1)
			case "no_pairs":  atomic.AddInt64(&cntHTTPOK, 1); atomic.AddInt64(&cntNoPairs, 1)
			case "chain":     atomic.AddInt64(&cntHTTPOK, 1); atomic.AddInt64(&cntChainMiss, 1)
			case "quote":     atomic.AddInt64(&cntHTTPOK, 1); atomic.AddInt64(&cntQuoteFilt, 1)
			case "liq":       atomic.AddInt64(&cntHTTPOK, 1); atomic.AddInt64(&cntLiqFloor, 1)
			case "consensus": atomic.AddInt64(&cntHTTPOK, 1); atomic.AddInt64(&cntConsensFL, 1)
			}
			results <- fetched{sym: cand.sym, info: info}
		}()
	}
	wg.Wait()
	close(results)

	dexBySym := make(map[string]*dexInfo, len(candidates))
	for r := range results {
		if r.info != nil {
			dexBySym[r.sym] = r.info
		}
	}

	log.L().Info().
		Int("scanned", len(candidates)).
		Int("hits", len(dexBySym)).
		Int64("http_err", cntHTTPErr).
		Int64("http_ok", cntHTTPOK).
		Int64("no_pairs", cntNoPairs).
		Int64("chain_miss", cntChainMiss).
		Int64("quote_filt", cntQuoteFilt).
		Int64("liq_floor", cntLiqFloor).
		Int64("consensus_fail", cntConsensFL).
		Msg("dex cycle complete")

	// Build opps.
	now := time.Now()
	c.mu.Lock()
	defer c.mu.Unlock()
	cutoff := now.Add(-dexOppPurgeAfter)
	for k, ts := range c.lastSeen {
		if ts.Before(cutoff) {
			delete(c.firstSeen, k)
			delete(c.lastSeen, k)
		}
	}

	opps := make([]map[string]any, 0, 256)
	for sym, dex := range dexBySym {
		perpByEx, ok := perpMap[sym]
		if !ok || dex.Price <= 0 {
			continue
		}
		for perpEx, p := range perpByEx {
			intH := p.IntervalH
			if intH <= 0 {
				intH = 8
			}
			rate8h := p.Rate * (8.0 / intH) * 100.0
			shortFunding := -rate8h
			basisPct := (p.MarkPrice - dex.Price) / dex.Price * 100.0
			if basisPct > maxBasisPct || basisPct < -maxBasisPct {
				continue
			}
			gross := shortFunding + basisPct

			key := dexKey{symbol: sym, perpEx: perpEx}
			first, seen := c.firstSeen[key]
			if !seen {
				c.firstSeen[key] = now
				c.lastSeen[key] = now
				continue
			}
			c.lastSeen[key] = now
			if now.Sub(first) < dexOppMinLifetime {
				continue
			}

			feeDexRT := dexFeeRoundtripPct
			feePerpRT := feeOf(perpEx) * 100 * 2
			totalFees := feeDexRT + feePerpRT
			net := gross - totalFees
			netAPR := 0.0
			if net > 0 {
				netAPR = net * (365.0 * 3.0)
			}
			opps = append(opps, map[string]any{
				"type":              "dex_short",
				"symbol":            sym,
				"dex_chain":         dex.Chain,
				"dex_name":          dex.Dex,
				"dex_pair_url":      dex.PairURL,
				"dex_base_address":  dex.BaseAddress,
				"short_exchange":    perpEx,
				"dex_price":         dex.Price,
				"perp_price":        p.MarkPrice,
				"dex_liquidity_usd": dex.LiquidityUSD,
				"dex_volume_usd":    dex.VolumeUSD,
				"perp_volume_usd":   p.Volume24h,
				"funding_rate":      p.Rate,
				"short_funding_8h":  shortFunding,
				"basis_pct":         basisPct,
				"gross":             gross,
				"fee_dex":           feeDexRT,
				"fee_perp":          feePerpRT,
				"total_fees":        totalFees,
				"net_profit":        net,
				"net_apr":           netAPR,
				"interval_h":        intH,
				"next_ts":           nextTsOf(p.NextFunding),
			})
		}
	}

	sort.Slice(opps, func(i, j int) bool {
		return opps[i]["net_profit"].(float64) > opps[j]["net_profit"].(float64)
	})
	if len(opps) > 200 {
		opps = opps[:200]
	}
	out := map[string]any{
		"opportunities":   opps,
		"generated_at":    now.Unix(),
		"symbols_scanned": len(candidates),
		"dex_hits":        len(dexBySym),
	}
	if err := writeAtomic(filepath.Join(c.cacheDir, "dex_arbitrage.json"), out); err != nil {
		log.L().Warn().Err(err).Msg("dex_arbitrage write failed")
	}
}

func (c *DEXCompute) writeEmpty() {
	out := map[string]any{
		"opportunities":   []map[string]any{},
		"generated_at":    time.Now().Unix(),
		"symbols_scanned": 0,
		"dex_hits":        0,
	}
	_ = writeAtomic(filepath.Join(c.cacheDir, "dex_arbitrage.json"), out)
}

// refreshCG pulls top-1000 markets and the full coins-list with platforms,
// merges into cgCache. 1h TTL.
func (c *DEXCompute) refreshCG(ctx context.Context) {
	c.mu.Lock()
	if !c.cgUpdated.IsZero() && time.Since(c.cgUpdated) < cgTTL && len(c.cgCache) > 0 {
		c.mu.Unlock()
		return
	}
	c.mu.Unlock()

	httpClient := &http.Client{Timeout: 30 * time.Second}

	// 1. coins/markets — top 1000 by mcap (4 pages × 250).
	rankByID := make(map[string]int, 1000)
	for page := 1; page <= 4; page++ {
		url := fmt.Sprintf("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page=%d&sparkline=false", page)
		req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
		if err != nil {
			continue
		}
		req.Header.Set("User-Agent", "Mozilla/5.0 avalant-fetcher/go")
		resp, err := httpClient.Do(req)
		if err != nil {
			log.L().Debug().Err(err).Int("page", page).Msg("CG markets failed")
			continue
		}
		var rows []struct {
			ID            string `json:"id"`
			MarketCapRank int    `json:"market_cap_rank"`
		}
		if err := sonic.ConfigStd.NewDecoder(resp.Body).Decode(&rows); err != nil {
			resp.Body.Close()
			continue
		}
		resp.Body.Close()
		for _, r := range rows {
			if r.ID != "" && r.MarketCapRank > 0 {
				rankByID[r.ID] = r.MarketCapRank
			}
		}
	}

	// 2. coins/list?include_platform=true — all coins with chain platforms.
	req, err := http.NewRequestWithContext(ctx, "GET",
		"https://api.coingecko.com/api/v3/coins/list?include_platform=true", nil)
	if err != nil {
		return
	}
	req.Header.Set("User-Agent", "Mozilla/5.0 avalant-fetcher/go")
	resp, err := httpClient.Do(req)
	if err != nil {
		log.L().Warn().Err(err).Msg("CG list failed — keeping stale cache")
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		log.L().Warn().Int("status", resp.StatusCode).Msg("CG list non-200 — keeping stale cache")
		return
	}
	var coins []struct {
		ID        string            `json:"id"`
		Symbol    string            `json:"symbol"`
		Platforms map[string]string `json:"platforms"`
	}
	if err := sonic.ConfigStd.NewDecoder(resp.Body).Decode(&coins); err != nil {
		log.L().Warn().Err(err).Msg("CG list decode failed")
		return
	}

	// Build cache: symbol → best entry (lowest mcap_rank with mappable platform).
	newCache := make(map[string]cgEntry, 4096)
	for _, c := range coins {
		sym := strings.ToUpper(c.Symbol)
		if sym == "" {
			continue
		}
		// Find mappable platform.
		var (
			pickChain    string
			pickContract string
		)
		// Prefer chains in chainPreference order.
		for _, pref := range chainPreference {
			for cgChain, addr := range c.Platforms {
				if cgToDS[cgChain] == pref && addr != "" {
					pickChain = pref
					pickContract = addr
					break
				}
			}
			if pickChain != "" {
				break
			}
		}
		if pickChain == "" {
			// Fallback: any mappable platform.
			for cgChain, addr := range c.Platforms {
				if ds, ok := cgToDS[cgChain]; ok && addr != "" {
					pickChain = ds
					pickContract = addr
					break
				}
			}
		}
		if pickChain == "" {
			continue
		}
		rank := rankByID[c.ID]
		if rank == 0 {
			rank = 10_000 // unranked
		}
		// Keep the highest-mcap winner per symbol.
		if existing, ok := newCache[sym]; ok && existing.mcapRank <= rank {
			continue
		}
		newCache[sym] = cgEntry{
			mcapRank: rank,
			chain:    pickChain,
			contract: strings.ToLower(pickContract),
		}
	}

	c.mu.Lock()
	c.cgCache = newCache
	c.cgUpdated = time.Now()
	c.mu.Unlock()
	log.L().Info().Int("symbols", len(newCache)).Msg("CG cache refreshed")
}

// fetchDexPool returns the best-liquidity pool for (chain, address) from
// DexScreener with cross-pool consensus check. Returns nil if no pool
// passes the liquidity / volume floors or all pools disagree.
func fetchDexPool(ctx context.Context, chain, address string) *dexInfo {
	info, _ := fetchDexPoolDbg(ctx, chain, address)
	return info
}

// fetchDexPoolDbg is the same as fetchDexPool but additionally returns a
// short string describing where the row was dropped. Used by the cycle
// instrumentation in tick(); production callers use fetchDexPool.
func fetchDexPoolDbg(ctx context.Context, chain, address string) (*dexInfo, string) {
	url := fmt.Sprintf("https://api.dexscreener.com/latest/dex/tokens/%s", address)
	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		log.L().Debug().Err(err).Str("addr", address).Msg("dex req-build err")
		return nil, "http_err"
	}
	req.Header.Set("User-Agent", "Mozilla/5.0 avalant-fetcher/go")
	httpClient := &http.Client{Timeout: 6 * time.Second}
	resp, err := httpClient.Do(req)
	if err != nil {
		log.L().Warn().Err(err).Str("addr", address).Msg("dex http-do err")
		return nil, "http_err"
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		log.L().Warn().Int("status", resp.StatusCode).Str("addr", address).Msg("dex http non-200")
		return nil, "http_err"
	}
	var doc struct {
		Pairs []struct {
			ChainID    string `json:"chainId"`
			DexID      string `json:"dexId"`
			URL        string `json:"url"`
			PriceUSD   string `json:"priceUsd"`
			BaseToken  struct {
				Symbol  string `json:"symbol"`
				Address string `json:"address"`
			} `json:"baseToken"`
			QuoteToken struct {
				Symbol string `json:"symbol"`
			} `json:"quoteToken"`
			Liquidity struct {
				USD float64 `json:"usd"`
			} `json:"liquidity"`
			Volume struct {
				H24 float64 `json:"h24"`
			} `json:"volume"`
			PairAddress string `json:"pairAddress"`
		} `json:"pairs"`
	}
	if err := sonic.ConfigStd.NewDecoder(resp.Body).Decode(&doc); err != nil {
		return nil, "http_err"
	}
	addrLow := strings.ToLower(address)

	type pool struct {
		symbol  string
		dex     string
		price   float64
		liq     float64
		vol     float64
		pairURL string
	}
	var (
		anyChain  bool // saw a pair on the requested chain (regardless of address match)
		anyMatch  bool // saw a pair on chain with matching base address
		anyQuote  bool // saw a pair with our base on chain & accepted quote
	)
	pools := make([]pool, 0, len(doc.Pairs))
	for _, p := range doc.Pairs {
		if p.ChainID != chain {
			continue
		}
		anyChain = true
		if strings.ToLower(p.BaseToken.Address) != addrLow {
			continue
		}
		anyMatch = true
		quoteSym := strings.ToUpper(p.QuoteToken.Symbol)
		if _, ok := acceptedQuotes[quoteSym]; !ok {
			continue
		}
		anyQuote = true
		px, _ := strconv.ParseFloat(p.PriceUSD, 64)
		if px <= 0 {
			continue
		}
		pools = append(pools, pool{
			symbol:  strings.ToUpper(p.BaseToken.Symbol),
			dex:     p.DexID,
			price:   px,
			liq:     p.Liquidity.USD,
			vol:     p.Volume.H24,
			pairURL: p.URL,
		})
	}
	if len(pools) == 0 {
		if !anyChain {
			return nil, "chain"
		}
		if !anyMatch {
			return nil, "no_pairs"
		}
		if !anyQuote {
			return nil, "quote"
		}
		return nil, "no_pairs"
	}
	eligible := make([]pool, 0, len(pools))
	for _, p := range pools {
		if p.liq >= minDEXLiqUSD && p.vol >= minDEXVolUSD {
			eligible = append(eligible, p)
		}
	}
	if len(eligible) == 0 {
		return nil, "liq"
	}
	// Consensus: median price of top-5 by liquidity; pick best whose
	// price is within max-dev of median.
	sort.Slice(pools, func(i, j int) bool { return pools[i].liq > pools[j].liq })
	voters := pools
	if len(voters) > 5 {
		voters = voters[:5]
	}
	prices := make([]float64, len(voters))
	for i, v := range voters {
		prices[i] = v.price
	}
	sort.Float64s(prices)
	median := prices[len(prices)/2]
	if median <= 0 {
		return nil, "consensus"
	}
	sort.Slice(eligible, func(i, j int) bool { return eligible[i].liq > eligible[j].liq })
	var best *pool
	for i := range eligible {
		if math.Abs(eligible[i].price-median)/median <= dexConsensusMaxDev {
			best = &eligible[i]
			break
		}
	}
	if best == nil {
		return nil, "consensus"
	}
	return &dexInfo{
		Symbol:       best.symbol,
		Chain:        chain,
		Dex:          best.dex,
		Price:        best.price,
		LiquidityUSD: best.liq,
		VolumeUSD:    best.vol,
		BaseAddress:  addrLow,
		PairURL:      best.pairURL,
	}, "ok"
}
