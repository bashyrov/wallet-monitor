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

	// Batched DexScreener fetch — the /tokens/<addr1>,<addr2>,...
	// endpoint accepts multiple addresses per call but the response
	// is CAPPED AT 30 PAIRS TOTAL regardless of how many addresses we
	// asked about. So with batchSize=30 each token gets ~1 pool on
	// average — kills the consensus check.
	//
	// batchSize=5 → ~30/5 = 6 pools per token average, enough for the
	// top-5 voter median. ~322 candidates / 5 = 65 batches per cycle =
	// ~130 req/min, safely under the 300/min public ceiling.
	const batchSize = 5
	dsCtx, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()

	// Index candidates by (chain, address). Same contract on a different
	// chain is a separate candidate entry, but addresses dedupe across
	// chains — DexScreener returns all pools for an address regardless
	// of chain, and we filter later.
	addrSet := make(map[string]struct{}, len(candidates))
	for _, c := range candidates {
		addrSet[c.entry.contract] = struct{}{}
	}
	addrs := make([]string, 0, len(addrSet))
	for a := range addrSet {
		addrs = append(addrs, a)
	}
	// Stable order so cache keys (later) are deterministic if we add caching.
	sort.Strings(addrs)

	// Fetch in batches with bounded concurrency. 4 parallel × 30 addrs
	// each at ~0.5s/call = ~5s for 11 batches; comfortable inside the
	// 60s context. Stays under 300 req/min by an order of magnitude.
	type batchResult struct {
		pairs []dsPair
		err   error
	}
	batches := make([][]string, 0, (len(addrs)+batchSize-1)/batchSize)
	for i := 0; i < len(addrs); i += batchSize {
		end := i + batchSize
		if end > len(addrs) {
			end = len(addrs)
		}
		batches = append(batches, addrs[i:end])
	}

	poolsByKey := make(map[string][]dsPair, len(addrs))
	var poolsMu sync.Mutex
	var wg sync.WaitGroup
	const workers = 4
	sem := make(chan struct{}, workers)
	var (
		cntBatchOK  int64
		cntBatch429 int64
		cntBatchErr int64
	)
	for _, batch := range batches {
		batch := batch
		wg.Add(1)
		sem <- struct{}{}
		go func() {
			defer wg.Done()
			defer func() { <-sem }()
			pairs, status, err := fetchDexBatch(dsCtx, batch)
			if err != nil {
				if status == 429 {
					atomic.AddInt64(&cntBatch429, 1)
				} else {
					atomic.AddInt64(&cntBatchErr, 1)
				}
				return
			}
			atomic.AddInt64(&cntBatchOK, 1)
			poolsMu.Lock()
			for _, p := range pairs {
				key := p.ChainID + ":" + strings.ToLower(p.BaseToken.Address)
				poolsByKey[key] = append(poolsByKey[key], p)
			}
			poolsMu.Unlock()
		}()
	}
	wg.Wait()

	// Per-candidate pool-pick + consensus, now from the in-memory map.
	dexBySym := make(map[string]*dexInfo, len(candidates))
	var (
		cntNoPairs   int64
		cntQuoteFilt int64
		cntLiqFloor  int64
		cntConsensus int64
	)
	for _, cand := range candidates {
		key := cand.entry.chain + ":" + cand.entry.contract
		pools := poolsByKey[key]
		if len(pools) == 0 {
			cntNoPairs++
			continue
		}
		info, reason := pickFromPools(cand.entry.chain, cand.entry.contract, pools)
		switch reason {
		case "ok":
			dexBySym[cand.sym] = info
		case "quote":
			cntQuoteFilt++
		case "liq":
			cntLiqFloor++
		case "consensus":
			cntConsensus++
		}
	}

	log.L().Info().
		Int("scanned", len(candidates)).
		Int("hits", len(dexBySym)).
		Int("batches", len(batches)).
		Int64("batch_ok", cntBatchOK).
		Int64("batch_429", cntBatch429).
		Int64("batch_err", cntBatchErr).
		Int64("no_pairs", cntNoPairs).
		Int64("quote_filt", cntQuoteFilt).
		Int64("liq_floor", cntLiqFloor).
		Int64("consensus_fail", cntConsensus).
		Msg("dex cycle complete")

	// If the cycle was completely starved (all batches errored), keep
	// the previous file rather than clobbering it with empty data.
	// Otherwise users see "DEX/Short — no opportunities" while we're
	// being rate-limited even though the data was fine 30s earlier.
	if cntBatchOK == 0 && len(dexBySym) == 0 {
		log.L().Warn().Msg("dex cycle starved (all batches failed) — keeping prior file")
		return
	}

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

// dsPair is the subset of DexScreener pair fields we care about. Decoded
// once per batch and held in memory for the consensus pass.
type dsPair struct {
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
}

// fetchDexBatch hits /tokens/<addr1>,<addr2>,... — DexScreener accepts up
// to 30 addresses per call, returning all pools across all chains for the
// requested tokens. Returns (pairs, statusCode, err).
func fetchDexBatch(ctx context.Context, addrs []string) ([]dsPair, int, error) {
	if len(addrs) == 0 {
		return nil, 0, nil
	}
	url := "https://api.dexscreener.com/latest/dex/tokens/" + strings.Join(addrs, ",")
	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return nil, 0, err
	}
	req.Header.Set("User-Agent", "Mozilla/5.0 avalant-fetcher/go")
	cl := &http.Client{Timeout: 8 * time.Second}
	resp, err := cl.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, resp.StatusCode, fmt.Errorf("status %d", resp.StatusCode)
	}
	var doc struct {
		Pairs []dsPair `json:"pairs"`
	}
	if err := sonic.ConfigStd.NewDecoder(resp.Body).Decode(&doc); err != nil {
		return nil, resp.StatusCode, err
	}
	return doc.Pairs, 200, nil
}

// pickFromPools applies the quote-filter / liquidity-floor / consensus
// check on a slice of pre-grouped pools (already filtered by chain+base
// address). Returns (info, reason). reason ∈ {"ok","quote","liq","consensus"}.
func pickFromPools(chain, address string, pairs []dsPair) (*dexInfo, string) {
	addrLow := strings.ToLower(address)

	type pool struct {
		symbol  string
		dex     string
		price   float64
		liq     float64
		vol     float64
		pairURL string
	}
	var anyQuote bool
	pools := make([]pool, 0, len(pairs))
	for _, p := range pairs {
		// Defence: caller already grouped by (chain, address) but we
		// re-verify so a key collision can't poison the result.
		if p.ChainID != chain {
			continue
		}
		if strings.ToLower(p.BaseToken.Address) != addrLow {
			continue
		}
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
		if !anyQuote {
			return nil, "quote"
		}
		return nil, "liq"
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
