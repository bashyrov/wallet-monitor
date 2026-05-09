package arb

import (
	"context"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/cache"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
)

// thin alias so parseFloat compiles (avoid `strconv.ParseFloat` literal).
var strconvParseFloat = strconv.ParseFloat

// SpotTicker — one symbol's spot price + 24h USD volume on one venue.
type spotTicker struct {
	Symbol    string  `json:"symbol"`
	Price     float64 `json:"price"`
	VolumeUSD float64 `json:"volume_usd"`
}

// Spot-fee map — same as Python's _SPOT_FEES (similar to perp fees but
// different per venue). Falls back to defaultFee.
var spotFees = map[string]float64{
	"binance": 0.001,
	"bybit":   0.001,
	"okx":     0.001,
	"gate":    0.001,
	"kucoin":  0.001,
	"mexc":    0.0005,
	"bitget":  0.001,
	"bingx":   0.001,
	"htx":     0.002,
}

func spotFeeOf(ex string) float64 {
	if v, ok := spotFees[ex]; ok {
		return v
	}
	return 0.001
}

// SpotCompute — periodic spot-arb compute. Polls each venue's REST
// ticker endpoint every 2s, joins with the futures funding store, writes
// spot_arbitrage.json.
type SpotCompute struct {
	store    *funding.Store
	books    *cache.Store
	cacheDir string
	interval time.Duration
}

func NewSpotCompute(store *funding.Store, books *cache.Store, cacheDir string, interval time.Duration) *SpotCompute {
	return &SpotCompute{store: store, books: books, cacheDir: cacheDir, interval: interval}
}

func (c *SpotCompute) Run(ctx context.Context) error {
	t := time.NewTicker(c.interval)
	defer t.Stop()
	// First tick after a small delay so funding store is non-empty.
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-time.After(3 * time.Second):
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

var spotExchanges = []string{
	"binance", "bybit", "okx", "gate", "kucoin",
	"mexc", "bitget", "bingx", "htx",
}

func (c *SpotCompute) tick(ctx context.Context) {
	// Parallel fetch all spot tickers.
	tickerCtx, cancel := context.WithTimeout(ctx, 8*time.Second)
	defer cancel()

	type result struct {
		ex      string
		tickers []spotTicker
	}
	results := make(chan result, len(spotExchanges))
	var wg sync.WaitGroup
	for _, ex := range spotExchanges {
		ex := ex
		wg.Add(1)
		go func() {
			defer wg.Done()
			tickers, err := fetchSpotTickers(tickerCtx, ex)
			if err != nil {
				log.L().Debug().Str("ex", ex).Err(err).Msg("spot fetch failed")
			}
			results <- result{ex: ex, tickers: tickers}
		}()
	}
	wg.Wait()
	close(results)

	// spot_map[symbol][exchange] = ticker
	spotMap := make(map[string]map[string]spotTicker, 1024)
	for r := range results {
		for _, t := range r.tickers {
			bucket, ok := spotMap[t.Symbol]
			if !ok {
				bucket = make(map[string]spotTicker, 4)
				spotMap[t.Symbol] = bucket
			}
			bucket[r.ex] = t
		}
	}

	// perp_map[symbol][exchange] = funding tick
	perpMap := make(map[string]map[string]funding.Tick, 1024)
	for ex, bucket := range c.store.SnapshotByExchange() {
		// Skip lighter — Python excludes it from spot pairing.
		if ex == "lighter" {
			continue
		}
		for sym, t := range bucket {
			if t.MarkPrice <= 0 || t.IntervalH <= 0 {
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

	// Cross-product: for each (spot venue × perp venue) on shared symbol,
	// emit a spot-short opp. Volume floor reuses the futures-side env
	// AVALANT_MIN_VOLUME_USD (default 0). Was a const tied to the old
	// hardcoded 20k constant; now it's just the var.
	minVolUSD := minVolumeUSD
	opps := make([]map[string]any, 0, 1024)
	for sym, spotByEx := range spotMap {
		perpByEx, ok := perpMap[sym]
		if !ok {
			continue
		}
		for spotEx, sd := range spotByEx {
			if sd.Price <= 0 || sd.VolumeUSD < minVolUSD {
				continue
			}
			for perpEx, pd := range perpByEx {
				if pd.MarkPrice <= 0 || pd.Rate == 0 || pd.Volume24h < minVolUSD {
					continue
				}
				intH := pd.IntervalH
				if intH <= 0 {
					intH = 8
				}
				rate8h := pd.Rate * (8.0 / intH) * 100.0
				shortFunding := rate8h
				basisPct := (pd.MarkPrice - sd.Price) / sd.Price * 100.0
				if basisPct > 100.0 || basisPct < -100.0 {
					continue
				}
				feeSpotRT := spotFeeOf(spotEx) * 100.0 * 2.0
				feePerpRT := feeOf(perpEx) * 100.0 * 2.0
				totalFees := feeSpotRT + feePerpRT
				// Bake top-of-book in/out — spot leg via <ex>_spot, perp
				// short via the bare exchange name. nil/null when either
				// side's book isn't subscribed (frontend hides those rows).
				inPct, outPct := ComputeInOutPair(c.books, spotEx+"_spot", perpEx, sym)
				// Net/8h uses live entry basis (in_pct) when available —
				// what an entry-now would actually capture — and falls back
				// to mark-based basisPct when orderbook tick missing. APR is
				// funding-only (no entry pickup) for sustainable annual view.
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
				opps = append(opps, map[string]any{
					"type":              "spot_short",
					"symbol":            sym,
					"spot_exchange":     spotEx,
					"short_exchange":    perpEx,
					"spot_price":        sd.Price,
					"perp_price":        pd.MarkPrice,
					"spot_volume_usd":   sd.VolumeUSD,
					"perp_volume_usd":   pd.Volume24h,
					"funding_rate":      pd.Rate,
					"short_funding_8h":  shortFunding,
					"basis_pct":         basisPct,
					"gross":             gross,
					"fee_spot":          feeSpotRT,
					"fee_perp":          feePerpRT,
					"total_fees":        totalFees,
					"net_profit":        net,
					"net_apr":           netAPR,
					"interval_h":        intH,
					"next_ts":           nextTsOf(pd.NextFunding),
					"in_pct":            inPct,
					"out_pct":           outPct,
				})
			}
		}
	}

	// Sort by |basis_pct| desc — same rule as futures L/S so a wide
	// basis lands in the tracked set regardless of net_profit (the
	// orderbook-subscribed set is what matters for entry; net_profit
	// is a lagging downstream metric). Cap at 1000 to match L/S.
	sort.Slice(opps, func(i, j int) bool {
		ai, _ := opps[i]["basis_pct"].(float64)
		aj, _ := opps[j]["basis_pct"].(float64)
		if ai < 0 {
			ai = -ai
		}
		if aj < 0 {
			aj = -aj
		}
		return ai > aj
	})
	if len(opps) > 1000 {
		opps = opps[:1000]
	}

	out := map[string]any{
		"opportunities":  opps,
		"generated_at":   time.Now().Unix(),
		"spot_exchanges": spotExchanges,
	}
	if err := writeAtomic(filepath.Join(c.cacheDir, "spot_arbitrage.json"), out); err != nil {
		log.L().Warn().Err(err).Msg("spot_arbitrage write failed")
	}
}

// ── per-venue REST fetchers ──────────────────────────────────────────────

func fetchSpotTickers(ctx context.Context, ex string) ([]spotTicker, error) {
	switch ex {
	case "binance":
		return fetchBinanceSpot(ctx)
	case "bybit":
		return fetchBybitSpot(ctx)
	case "okx":
		return fetchOKXSpot(ctx)
	case "gate":
		return fetchGateSpot(ctx)
	case "kucoin":
		return fetchKuCoinSpot(ctx)
	case "mexc":
		return fetchMEXCSpot(ctx)
	case "bitget":
		return fetchBitgetSpot(ctx)
	case "bingx":
		return fetchBingXSpot(ctx)
	case "htx":
		return fetchHTXSpot(ctx)
	}
	return nil, nil
}

func fetchBinanceSpot(ctx context.Context) ([]spotTicker, error) {
	var rows []struct {
		Symbol      string `json:"symbol"`
		LastPrice   string `json:"lastPrice"`
		QuoteVolume string `json:"quoteVolume"`
	}
	if err := funding.HTTPGet(ctx, "https://api.binance.com/api/v3/ticker/24hr", &rows); err != nil {
		return nil, err
	}
	out := make([]spotTicker, 0, len(rows))
	for _, r := range rows {
		if !strings.HasSuffix(r.Symbol, "USDT") {
			continue
		}
		px, vol := parseFloat(r.LastPrice), parseFloat(r.QuoteVolume)
		if px > 0 && vol > 0 {
			out = append(out, spotTicker{Symbol: strings.TrimSuffix(r.Symbol, "USDT"), Price: px, VolumeUSD: vol})
		}
	}
	return out, nil
}

func fetchBybitSpot(ctx context.Context) ([]spotTicker, error) {
	var doc struct {
		Result struct {
			List []struct {
				Symbol      string `json:"symbol"`
				LastPrice   string `json:"lastPrice"`
				Turnover24h string `json:"turnover24h"`
			} `json:"list"`
		} `json:"result"`
	}
	if err := funding.HTTPGet(ctx, "https://api.bybit.com/v5/market/tickers?category=spot", &doc); err != nil {
		return nil, err
	}
	out := make([]spotTicker, 0, len(doc.Result.List))
	for _, r := range doc.Result.List {
		if !strings.HasSuffix(r.Symbol, "USDT") {
			continue
		}
		px, vol := parseFloat(r.LastPrice), parseFloat(r.Turnover24h)
		if px > 0 && vol > 0 {
			out = append(out, spotTicker{Symbol: strings.TrimSuffix(r.Symbol, "USDT"), Price: px, VolumeUSD: vol})
		}
	}
	return out, nil
}

func fetchOKXSpot(ctx context.Context) ([]spotTicker, error) {
	var doc struct {
		Data []struct {
			InstID    string `json:"instId"`
			Last      string `json:"last"`
			VolCcy24h string `json:"volCcy24h"`
		} `json:"data"`
	}
	if err := funding.HTTPGet(ctx, "https://www.okx.com/api/v5/market/tickers?instType=SPOT", &doc); err != nil {
		return nil, err
	}
	out := make([]spotTicker, 0, len(doc.Data))
	for _, r := range doc.Data {
		if !strings.HasSuffix(r.InstID, "-USDT") {
			continue
		}
		px := parseFloat(r.Last)
		volBase := parseFloat(r.VolCcy24h)
		// OKX volCcy24h is in base currency units; convert to USDT via px.
		vol := volBase * px
		if px > 0 && vol > 0 {
			out = append(out, spotTicker{Symbol: strings.TrimSuffix(r.InstID, "-USDT"), Price: px, VolumeUSD: vol})
		}
	}
	return out, nil
}

func fetchGateSpot(ctx context.Context) ([]spotTicker, error) {
	var rows []struct {
		CurrencyPair string `json:"currency_pair"`
		Last         string `json:"last"`
		QuoteVolume  string `json:"quote_volume"`
	}
	if err := funding.HTTPGet(ctx, "https://api.gateio.ws/api/v4/spot/tickers", &rows); err != nil {
		return nil, err
	}
	out := make([]spotTicker, 0, len(rows))
	for _, r := range rows {
		if !strings.HasSuffix(r.CurrencyPair, "_USDT") {
			continue
		}
		px, vol := parseFloat(r.Last), parseFloat(r.QuoteVolume)
		if px > 0 && vol > 0 {
			out = append(out, spotTicker{Symbol: strings.TrimSuffix(r.CurrencyPair, "_USDT"), Price: px, VolumeUSD: vol})
		}
	}
	return out, nil
}

func fetchKuCoinSpot(ctx context.Context) ([]spotTicker, error) {
	var doc struct {
		Data struct {
			Ticker []struct {
				Symbol   string `json:"symbol"`
				Last     string `json:"last"`
				VolValue string `json:"volValue"`
			} `json:"ticker"`
		} `json:"data"`
	}
	if err := funding.HTTPGet(ctx, "https://api.kucoin.com/api/v1/market/allTickers", &doc); err != nil {
		return nil, err
	}
	out := make([]spotTicker, 0, len(doc.Data.Ticker))
	for _, r := range doc.Data.Ticker {
		if !strings.HasSuffix(r.Symbol, "-USDT") {
			continue
		}
		px, vol := parseFloat(r.Last), parseFloat(r.VolValue)
		if px > 0 && vol > 0 {
			out = append(out, spotTicker{Symbol: strings.TrimSuffix(r.Symbol, "-USDT"), Price: px, VolumeUSD: vol})
		}
	}
	return out, nil
}

func fetchMEXCSpot(ctx context.Context) ([]spotTicker, error) {
	var rows []struct {
		Symbol      string `json:"symbol"`
		LastPrice   string `json:"lastPrice"`
		QuoteVolume string `json:"quoteVolume"`
	}
	if err := funding.HTTPGet(ctx, "https://api.mexc.com/api/v3/ticker/24hr", &rows); err != nil {
		return nil, err
	}
	out := make([]spotTicker, 0, len(rows))
	for _, r := range rows {
		if !strings.HasSuffix(r.Symbol, "USDT") {
			continue
		}
		px, vol := parseFloat(r.LastPrice), parseFloat(r.QuoteVolume)
		if px > 0 && vol > 0 {
			out = append(out, spotTicker{Symbol: strings.TrimSuffix(r.Symbol, "USDT"), Price: px, VolumeUSD: vol})
		}
	}
	return out, nil
}

func fetchBitgetSpot(ctx context.Context) ([]spotTicker, error) {
	var doc struct {
		Data []struct {
			Symbol      string `json:"symbol"`
			LastPr      string `json:"lastPr"`
			USDTVolume  string `json:"usdtVolume"`
			QuoteVolume string `json:"quoteVolume"`
		} `json:"data"`
	}
	if err := funding.HTTPGet(ctx, "https://api.bitget.com/api/v2/spot/market/tickers", &doc); err != nil {
		return nil, err
	}
	out := make([]spotTicker, 0, len(doc.Data))
	for _, r := range doc.Data {
		if !strings.HasSuffix(r.Symbol, "USDT") {
			continue
		}
		px := parseFloat(r.LastPr)
		vol := parseFloat(r.USDTVolume)
		if vol == 0 {
			vol = parseFloat(r.QuoteVolume)
		}
		if px > 0 && vol > 0 {
			out = append(out, spotTicker{Symbol: strings.TrimSuffix(r.Symbol, "USDT"), Price: px, VolumeUSD: vol})
		}
	}
	return out, nil
}

func fetchBingXSpot(ctx context.Context) ([]spotTicker, error) {
	var doc struct {
		Data []struct {
			Symbol      string `json:"symbol"`
			LastPrice   string `json:"lastPrice"`
			QuoteVolume string `json:"quoteVolume"`
		} `json:"data"`
	}
	if err := funding.HTTPGet(ctx, "https://open-api.bingx.com/openApi/spot/v1/ticker/24hr", &doc); err != nil {
		return nil, err
	}
	out := make([]spotTicker, 0, len(doc.Data))
	for _, r := range doc.Data {
		if !strings.HasSuffix(r.Symbol, "-USDT") {
			continue
		}
		px, vol := parseFloat(r.LastPrice), parseFloat(r.QuoteVolume)
		if px > 0 && vol > 0 {
			out = append(out, spotTicker{Symbol: strings.TrimSuffix(r.Symbol, "-USDT"), Price: px, VolumeUSD: vol})
		}
	}
	return out, nil
}

func fetchHTXSpot(ctx context.Context) ([]spotTicker, error) {
	var doc struct {
		Data []struct {
			Symbol string  `json:"symbol"`
			Close  float64 `json:"close"`
			Vol    float64 `json:"vol"`
		} `json:"data"`
	}
	if err := funding.HTTPGet(ctx, "https://api.huobi.pro/market/tickers", &doc); err != nil {
		return nil, err
	}
	out := make([]spotTicker, 0, len(doc.Data))
	for _, r := range doc.Data {
		s := strings.ToLower(r.Symbol)
		if !strings.HasSuffix(s, "usdt") {
			continue
		}
		if r.Close > 0 && r.Vol > 0 {
			out = append(out, spotTicker{
				Symbol:    strings.ToUpper(strings.TrimSuffix(s, "usdt")),
				Price:     r.Close,
				VolumeUSD: r.Vol,
			})
		}
	}
	return out, nil
}

// parseFloat — safe ParseFloat that returns 0 on error or empty input.
func parseFloat(s string) float64 {
	if s == "" {
		return 0
	}
	v, err := strconvParseFloat(s, 64)
	if err != nil {
		return 0
	}
	return v
}
