// Package ethereal — funding adapter for Ethereal perp-DEX.
//
// Two REST calls joined by product ID:
//   GET https://api.ethereal.trade/v1/product
//     → paginated list of products with id, baseTokenName, fundingRate1h,
//       status ("ACTIVE"), openInterest.
//   GET https://api.ethereal.trade/v1/product/market-price
//       ?productIds[]=<id>&productIds[]=<id>...
//     → [{productId, oraclePrice, bestBidPrice, bestAskPrice}]
//
// Only ACTIVE products are kept. Funding rate is 1h (field fundingRate1h).
// Oracle price is used as mark price. Volume is not exposed publicly — 0.
// Interval: 1h; next-funding boundary = top of next hour UTC.
//
// The Python fetcher uses the proprietary ethereal-sdk which is just an
// HTTP wrapper around these endpoints. We call them directly.
package ethereal

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/bytedance/sonic"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/funding"
)

const baseURL = "https://api.ethereal.trade"

type Adapter struct{}

func New() *Adapter { return &Adapter{} }

func (a *Adapter) Name() string                             { return "ethereal" }
func (a *Adapter) URL(_ context.Context) (string, error)   { return "", nil }
func (a *Adapter) BuildSubscribe(_ []string) [][]byte      { return nil }
func (a *Adapter) ParseWS(_ []byte) ([]funding.Tick, error) { return nil, nil }
func (a *Adapter) Heartbeat() []byte                       { return nil }
func (a *Adapter) HeartbeatInterval() time.Duration        { return 0 }
func (a *Adapter) PongFor(_ []byte) []byte                 { return nil }
func (a *Adapter) UseLibPings() bool                       { return false }
func (a *Adapter) DecompressGzip() bool                    { return false }

type productItem struct {
	ID            string      `json:"id"`
	BaseTokenName string      `json:"baseTokenName"`
	FundingRate1h interface{} `json:"fundingRate1h"` // API returns string
	Status        string      `json:"status"`
	OpenInterest  interface{} `json:"openInterest"` // API returns string
}

func (a *Adapter) BackstopFetch(ctx context.Context, _ []string) ([]funding.Tick, error) {
	// Fetch all pages of /v1/product
	products, err := fetchProducts(ctx)
	if err != nil {
		return nil, err
	}
	if len(products) == 0 {
		return nil, errors.New("ethereal: no products")
	}

	// Build id list for market-price query
	ids := make([]string, 0, len(products))
	for _, p := range products {
		if p.Status == "ACTIVE" && p.FundingRate1h != 0 {
			ids = append(ids, p.ID)
		}
	}
	if len(ids) == 0 {
		return nil, errors.New("ethereal: no active products")
	}

	// GET /v1/product/market-price?productIds[]=id1&productIds[]=id2...
	priceURL := buildPriceURL(ids)
	body, err := getJSON(ctx, priceURL)
	if err != nil {
		return nil, err
	}
	var priceResp struct {
		Data []struct {
			ProductID   string      `json:"productId"`
			OraclePrice interface{} `json:"oraclePrice"` // API returns string
		} `json:"data"`
	}
	if err := sonic.Unmarshal(body, &priceResp); err != nil {
		return nil, err
	}
	priceByID := make(map[string]float64, len(priceResp.Data))
	for _, mp := range priceResp.Data {
		if p := funding.ParseFloat(mp.OraclePrice); p > 0 {
			priceByID[mp.ProductID] = p
		}
	}

	now := time.Now().Unix()
	nextFunding := time.Unix((now/3600+1)*3600, 0)

	out := make([]funding.Tick, 0, len(products))
	for _, p := range products {
		rate := funding.ParseFloat(p.FundingRate1h)
		if p.Status != "ACTIVE" || rate == 0 || p.BaseTokenName == "" {
			continue
		}
		price := priceByID[p.ID]
		if price <= 0 {
			continue
		}
		oi := funding.ParseFloat(p.OpenInterest)
		out = append(out, funding.Tick{
			Symbol:      strings.ToUpper(p.BaseTokenName),
			Rate:        rate,
			MarkPrice:   price,
			OpenIntUSD:  oi * price,
			NextFunding: nextFunding,
			IntervalH:   1.0,
		})
	}
	if len(out) == 0 {
		return nil, errors.New("ethereal: empty results after price join")
	}
	return out, nil
}

func (a *Adapter) BackstopInterval() time.Duration { return 5 * time.Minute }

func fetchProducts(ctx context.Context) ([]productItem, error) {
	// The API is paginated; fetch first page (limit=100). In practice
	// Ethereal has <20 products so one page is sufficient.
	body, err := getJSON(ctx, baseURL+"/v1/product?limit=100")
	if err != nil {
		return nil, err
	}
	var resp struct {
		Data []productItem `json:"data"`
	}
	if err := sonic.Unmarshal(body, &resp); err != nil {
		return nil, err
	}
	return resp.Data, nil
}

func buildPriceURL(ids []string) string {
	var sb strings.Builder
	sb.WriteString(baseURL + "/v1/product/market-price?")
	for i, id := range ids {
		if i > 0 {
			sb.WriteByte('&')
		}
		sb.WriteString(fmt.Sprintf("productIds[]=%s", id))
	}
	return sb.String()
}

func getJSON(ctx context.Context, url string) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", "Mozilla/5.0 avalant-fetcher/go")
	cl := &http.Client{Timeout: 10 * time.Second}
	resp, err := cl.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, errors.New("http " + resp.Status)
	}
	return io.ReadAll(resp.Body)
}
