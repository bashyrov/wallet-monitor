package arb

import (
	"context"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/bytedance/sonic"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/binancealpha"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/okxdex"
)

// CexMatch is the verdict compute receives from the address registry
// closure. Deposit/Withdraw are tri-state (*bool, nil = unknown) and
// must be surfaced as "unknown" in the UI — never assumed enabled.
// Same rule as AddressKnown: the user is making a transfer decision,
// false-positive on transfer availability is a high-cost error.
type CexMatch struct {
	Verified     bool
	MatchChain   string
	AddressKnown bool
	Deposit      *bool
	Withdraw     *bool
}

// CexAddressMatcher is the read closure compute uses against the CEX
// assets registry. main.go builds it from cex_assets.Registry — the
// arb package itself doesn't import cex_assets to keep this layer
// transport-agnostic and the dep graph one-directional.
//
// Returns: verified (chain AND address both match the venue's record),
// matchChain (canonical chain id if verified, else ""), and
// addressKnown (ticker exists in venue registry — distinguishes
// "verified false because address differs" from "we have no data").
type CexAddressMatcher func(venue, ticker, dexChain, dexAddress string) CexMatch

// dexInfo is the resolved on-chain placement + price for a symbol.
// Dex is "Binance Alpha" when the symbol resolves via the Alpha token
// list (price + real liquidity in one payload) and "OKX" otherwise
// (aggregator-level price, LiquidityUSD borrowed from the DexScreener
// parallel feed or 0). Downstream consumers treat 0 as "unknown", not
// "illiquid".
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

// DEXCompute builds dex-short opportunities from the OKX Web3 DEX API
// (sole on-chain source): symbol→contract resolution via the hourly
// okxdex token sweep, prices via one batched /dex/market/price call per
// cycle, funding join from the store, writes dex_arbitrage.json.
type DEXCompute struct {
	store    *funding.Store
	books    *cache.Store
	cacheDir string
	interval time.Duration
	okx      *okxdex.Service
	alpha    *binancealpha.Service

	mu        sync.Mutex
	firstSeen map[dexKey]time.Time
	lastSeen  map[dexKey]time.Time

	// dexSnap holds the most recent successful dexBySym snapshot so the
	// dex_spot compute can join CEX spot prices against it without an
	// extra OKX price sweep. Updated at the end of every successful tick
	// under snapMu. Empty/stale snapshot still serves reads — a starved
	// cycle keeps the previous map.
	snapMu  sync.RWMutex
	dexSnap map[string]dexInfo

	// cexMatcher is set when AVALANT_CEX_ASSETS=1. When nil, every row
	// emits address_verified=false unconditionally.
	cexMatcher CexAddressMatcher

	// dexScreenerLiq is the last-read liquidity snapshot from the parallel
	// DexScreener-based compute (dex_screener_arbitrage.json). OKX's DEX
	// API doesn't expose per-pool liquidity, so we borrow it from
	// DexScreener when the same (symbol, chain) is present in that feed.
	// Key = "<UPPER(symbol)>|<lower(chain)>". Refreshed inside tick() at
	// the start of each cycle. Nil when the parallel file doesn't exist.
	liqMu           sync.RWMutex
	dexScreenerLiq  map[string]float64
	dexScreenerLiqM time.Time
}

// SetCexRegistry wires the address-match closure. Safe to call before
// or after Run is started — the compute reads c.cexMatcher under the
// mu lock only inside tick().
func (c *DEXCompute) SetCexRegistry(m CexAddressMatcher) {
	c.mu.Lock()
	c.cexMatcher = m
	c.mu.Unlock()
}

type dexKey struct {
	symbol string
	perpEx string
}

const (
	dexOppMinLifetime  = 25 * time.Second
	dexOppPurgeAfter   = 5 * time.Minute
	maxBasisPct        = 100.0
	symbolBatchLimit   = 900
	maxRefsPerSymbol   = 5
	dexFeeRoundtripPct = 0.6 + 0.2 // 0.3%×2 swap + 0.2% slippage
)

// okxChainCanonical maps OKX chainIndex → the canonical lowercase chain
// id the rest of the pipeline speaks (cex_assets matching + frontend
// chain labels kept identical to the pre-OKX DexScreener ids).
var okxChainCanonical = map[string]string{
	"1":      "ethereum",
	"56":     "bsc",
	"137":    "polygon",
	"42161":  "arbitrum",
	"10":     "optimism",
	"8453":   "base",
	"43114":  "avalanche",
	"250":    "fantom",
	"324":    "zksync",
	"59144":  "linea",
	"534352": "scroll",
	"5000":   "mantle",
	"81457":  "blast",
	"501":    "solana",
	"195":    "tron",
	"607":    "ton",
	"784":    "sui",
	"637":    "aptos",
}

var chainPreference = []string{
	"ethereum", "solana", "base", "arbitrum", "bsc", "polygon",
	"optimism", "avalanche", "blast", "linea", "scroll", "mantle", "sui", "ton",
}

var chainPrefRank = func() map[string]int {
	m := make(map[string]int, len(chainPreference))
	for i, c := range chainPreference {
		m[c] = i
	}
	return m
}()

func canonicalChain(ref okxdex.TokenRef) string {
	if c, ok := okxChainCanonical[ref.ChainIndex]; ok {
		return c
	}
	return strings.ReplaceAll(strings.ToLower(ref.ChainName), " ", "")
}

func NewDEXCompute(store *funding.Store, books *cache.Store, cacheDir string, interval time.Duration, okx *okxdex.Service, alpha *binancealpha.Service) *DEXCompute {
	return &DEXCompute{
		store:     store,
		books:     books,
		cacheDir:  cacheDir,
		interval:  interval,
		okx:       okx,
		alpha:     alpha,
		firstSeen: make(map[dexKey]time.Time),
		lastSeen:  make(map[dexKey]time.Time),
	}
}

func (c *DEXCompute) Run(ctx context.Context) error {
	t := time.NewTicker(c.interval)
	defer t.Stop()
	// First compute after a delay so the funding store is ready.
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

// refreshDexScreenerLiquidity reads the parallel DexScreener output
// once per compute cycle and rebuilds the (symbol,chain) → liquidity
// index. Cheap: file is ~150KB, gets read every 10s. Silent on error —
// a missing/stale file just means "no DexScreener enrichment this
// tick", rows keep dex_liquidity_usd=0.
func (c *DEXCompute) refreshDexScreenerLiquidity() {
	path := filepath.Join(c.cacheDir, "dex_screener_arbitrage.json")
	st, err := os.Stat(path)
	if err != nil {
		return
	}
	// Skip re-parse if the file mtime hasn't advanced since last read.
	c.liqMu.RLock()
	if !st.ModTime().After(c.dexScreenerLiqM) && c.dexScreenerLiq != nil {
		c.liqMu.RUnlock()
		return
	}
	c.liqMu.RUnlock()
	raw, err := os.ReadFile(path)
	if err != nil {
		return
	}
	var doc struct {
		Opportunities []struct {
			Symbol      string  `json:"symbol"`
			DexChain    string  `json:"dex_chain"`
			LiquidityUS float64 `json:"dex_liquidity_usd"`
		} `json:"opportunities"`
	}
	if err := sonic.Unmarshal(raw, &doc); err != nil {
		return
	}
	m := make(map[string]float64, len(doc.Opportunities))
	for _, r := range doc.Opportunities {
		if r.LiquidityUS <= 0 {
			continue
		}
		k := strings.ToUpper(strings.TrimSpace(r.Symbol)) + "|" + strings.ToLower(strings.TrimSpace(r.DexChain))
		if prev, ok := m[k]; !ok || r.LiquidityUS > prev {
			m[k] = r.LiquidityUS
		}
	}
	c.liqMu.Lock()
	c.dexScreenerLiq = m
	c.dexScreenerLiqM = st.ModTime()
	c.liqMu.Unlock()
}

func (c *DEXCompute) lookupDexScreenerLiquidity(symbol, chain string) float64 {
	c.liqMu.RLock()
	defer c.liqMu.RUnlock()
	if c.dexScreenerLiq == nil {
		return 0
	}
	return c.dexScreenerLiq[strings.ToUpper(symbol)+"|"+strings.ToLower(chain)]
}

func (c *DEXCompute) tick(ctx context.Context) {
	c.refreshDexScreenerLiquidity()
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

	// Resolve via Binance Alpha first — price + real per-token liquidity
	// ride the token-list payload, no per-cycle price sweep, no paywall
	// risk. A symbol listed on multiple chains resolves to the deepest
	// pool. Symbols Alpha doesn't cover fall through to OKX below.
	alphaBySym := make(map[string]*dexInfo, 256)
	if c.alpha != nil {
		for sym := range perpMap {
			refs := c.alpha.LookupBySymbol(sym)
			if len(refs) == 0 {
				continue
			}
			best := refs[0]
			for _, r := range refs[1:] {
				if r.LiquidityUSD > best.LiquidityUSD {
					best = r
				}
			}
			liq := best.LiquidityUSD
			if liq <= 0 {
				liq = c.lookupDexScreenerLiquidity(sym, best.Chain)
			}
			alphaBySym[sym] = &dexInfo{
				Symbol:       sym,
				Chain:        best.Chain,
				Dex:          "Binance Alpha",
				Price:        best.Price,
				LiquidityUSD: liq,
				VolumeUSD:    best.VolumeUSD,
				BaseAddress:  best.Address,
				PairURL:      "https://www.binance.com/en/alpha/" + best.Chain + "/" + best.Address,
			}
		}
	}

	// Resolve the remaining symbols against the OKX token map. Refs are
	// ordered by chain preference so majors resolve on their native chain
	// first.
	type candidate struct {
		sym  string
		refs []okxdex.TokenRef
	}
	candidates := make([]candidate, 0, len(perpMap))
	for sym := range perpMap {
		if _, ok := alphaBySym[sym]; ok {
			continue
		}
		refs := c.okx.LookupBySymbol(sym)
		if len(refs) == 0 {
			continue
		}
		ordered := make([]okxdex.TokenRef, len(refs))
		copy(ordered, refs)
		sort.SliceStable(ordered, func(i, j int) bool {
			ri, iOK := chainPrefRank[canonicalChain(ordered[i])]
			rj, jOK := chainPrefRank[canonicalChain(ordered[j])]
			if iOK != jOK {
				return iOK
			}
			return ri < rj
		})
		if len(ordered) > maxRefsPerSymbol {
			ordered = ordered[:maxRefsPerSymbol]
		}
		candidates = append(candidates, candidate{sym: sym, refs: ordered})
	}
	if len(candidates) == 0 && len(alphaBySym) == 0 {
		// Both token maps empty (creds missing / initial sweeps failed)
		// or no funding symbol exists on-chain — emit structure, no rows.
		c.writeEmpty()
		return
	}
	sort.Slice(candidates, func(i, j int) bool { return candidates[i].sym < candidates[j].sym })
	if len(candidates) > symbolBatchLimit {
		candidates = candidates[:symbolBatchLimit]
	}

	reqs := make([]okxdex.PriceReq, 0, len(candidates)*2)
	for _, cand := range candidates {
		for _, ref := range cand.refs {
			reqs = append(reqs, okxdex.PriceReq{ChainIndex: ref.ChainIndex, TokenContractAddress: ref.Address})
		}
	}
	var prices map[string]float64
	var pxErr error
	if len(reqs) > 0 {
		pxCtx, cancel := context.WithTimeout(ctx, 60*time.Second)
		prices, pxErr = c.okx.FetchPrices(pxCtx, reqs)
		cancel()
	}

	// If the cycle was completely starved (every OKX price chunk failed
	// AND Alpha resolved nothing), keep the previous file rather than
	// clobbering it with empty data. Otherwise users see "DEX/Short — no
	// opportunities" during an OKX blip even though the data was fine
	// 30s earlier. With Alpha rows in hand we proceed — the OKX-only
	// symbols drop out for one tick, which beats an all-stale file.
	if len(prices) == 0 && pxErr != nil && len(alphaBySym) == 0 {
		log.L().Warn().Err(pxErr).Msg("dex cycle starved (OKX price fetch failed) — keeping prior file")
		return
	}

	// TODO(liquidity): OKX doesn't expose per-pool liquidity, so a symbol
	// living on multiple chains resolves to the first preference-ordered
	// ref with a non-zero price instead of the deepest pool.
	dexBySym := make(map[string]*dexInfo, len(candidates)+len(alphaBySym))
	for sym, info := range alphaBySym {
		dexBySym[sym] = info
	}
	for _, cand := range candidates {
		for _, ref := range cand.refs {
			px, ok := prices[okxdex.PriceKey(ref.ChainIndex, ref.Address)]
			if !ok || px <= 0 {
				continue
			}
			slug := strings.ReplaceAll(strings.ToLower(ref.ChainName), " ", "-")
			chain := canonicalChain(ref)
			dexBySym[cand.sym] = &dexInfo{
				Symbol:       cand.sym,
				Chain:        chain,
				Dex:          "OKX",
				Price:        px,
				BaseAddress:  ref.Address,
				PairURL:      "https://web3.okx.com/token/" + slug + "/" + ref.Address,
				LiquidityUSD: c.lookupDexScreenerLiquidity(cand.sym, chain),
			}
			break
		}
	}

	log.L().Info().
		Int("scanned", len(candidates)+len(alphaBySym)).
		Int("alpha_hits", len(alphaBySym)).
		Int("hits", len(dexBySym)).
		Int("price_reqs", len(reqs)).
		Int("prices", len(prices)).
		Bool("degraded", pxErr != nil).
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
			// Bake top-of-book in/out — DEX side has no orderbook adapter
			// (single aggregator price from OKX), so the perp short leg
			// alone drives the live values.
			inPct, outPct := ComputeInOutDex(c.books, perpEx, sym, dex.Price)
			// Net/8h uses live in_pct when available; falls back to mark-
			// based basisPct otherwise. APR is funding-only — sustainable
			// annual return without one-shot entry pickup.
			entryBasis := basisPct
			if inPct != nil {
				entryBasis = *inPct
			}
			gross := shortFunding + entryBasis
			net := gross - totalFees
			fundingOnly := shortFunding - totalFees
			netAPR := 0.0
			if fundingOnly > 0 {
				netAPR = fundingOnly * (365.0 * 3.0)
			}
			// Address verification. dex_short uses perpEx as the CEX
			// venue. Only 3 public adapters in v1 (gate/kucoin/bitget);
			// every other venue returns AddressKnown=false → verified
			// also false → frontend renders ⚠ unverified pill. Same
			// policy when AVALANT_CEX_ASSETS=0 (registry empty).
			var match CexMatch
			if c.cexMatcher != nil {
				match = c.cexMatcher(perpEx, sym, dex.Chain, dex.BaseAddress)
			}
			opps = append(opps, map[string]any{
				"type":                "dex_short",
				"symbol":              sym,
				"dex_chain":           dex.Chain,
				"dex_name":            dex.Dex,
				"dex_pair_url":        dex.PairURL,
				"dex_base_address":    dex.BaseAddress,
				"short_exchange":      perpEx,
				"dex_price":           dex.Price,
				"perp_price":          p.MarkPrice,
				"dex_liquidity_usd":   dex.LiquidityUSD,
				"dex_volume_usd":      dex.VolumeUSD,
				"perp_volume_usd":     p.Volume24h,
				"funding_rate":        p.Rate,
				"short_funding_8h":    shortFunding,
				"basis_pct":           basisPct,
				"gross":               gross,
				"fee_dex":             feeDexRT,
				"fee_perp":            feePerpRT,
				"total_fees":          totalFees,
				"net_profit":          net,
				"net_apr":             netAPR,
				"interval_h":          intH,
				"next_ts":             nextTsOf(p.NextFunding),
				"in_pct":              inPct,
				"out_pct":             outPct,
				"address_verified":    match.Verified,
				"address_match_chain": match.MatchChain,
				"address_known":       match.AddressKnown,
				// tri-state per-network status (nil → JSON null = UI "unknown")
				"cex_deposit":  match.Deposit,
				"cex_withdraw": match.Withdraw,
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
		"symbols_scanned": len(candidates) + len(alphaBySym),
		"dex_hits":        len(dexBySym),
	}
	if err := writeAtomic(filepath.Join(c.cacheDir, "dex_arbitrage.json"), out); err != nil {
		log.L().Warn().Err(err).Msg("dex_arbitrage write failed")
	}

	// Publish snapshot for DexSpotCompute (and any other consumer).
	// Deref the *dexInfo into a value so the consumer can't observe
	// mid-tick mutations on the upstream pointer (the next cycle may
	// overwrite entries in-place).
	snap := make(map[string]dexInfo, len(dexBySym))
	for k, v := range dexBySym {
		if v != nil {
			snap[k] = *v
		}
	}
	c.snapMu.Lock()
	c.dexSnap = snap
	c.snapMu.Unlock()
}

// SnapshotDexBySym returns a shallow copy of the most recent dexBySym
// produced by tick(). Consumed by DexSpotCompute. Safe for concurrent
// reads — returns a new map each call.
func (c *DEXCompute) SnapshotDexBySym() map[string]dexInfo {
	c.snapMu.RLock()
	defer c.snapMu.RUnlock()
	if c.dexSnap == nil {
		return nil
	}
	out := make(map[string]dexInfo, len(c.dexSnap))
	for k, v := range c.dexSnap {
		out[k] = v
	}
	return out
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
