// Package canonical normalises caller-side `limit` values to the discrete
// sets that each venue actually accepts. Without this round-up, exchanges
// silently return empty books for non-canonical limits — the behaviour we
// fought through 4 separate bugs in Python (Aster fork, Binance, Bitget,
// MEXC).
//
// Rule: round UP to the smallest accepted value. Serving 20 levels when the
// caller asked for 12 is strictly better than serving zero.
package canonical

// Set of valid `limit` values per exchange. Verbatim from
// backend/services/orderbook_cache.py:_VALID_LIMITS — keep in sync.
var validLimits = map[string][]int{
	"binance":      {5, 10, 20, 50, 100, 500, 1000},
	"aster":        {5, 10, 20, 50, 100, 500, 1000}, // Binance fork
	"bybit":        {1, 50, 200, 500, 1000},
	"bitget":       {5, 15, 50, 100, 200, 1000},
	"mexc":         {5, 10, 20, 50, 100, 200, 500, 1000},
	"okx":          {1, 5, 10, 20, 50, 100, 200, 400},
	"gate":         {5, 10, 20, 50, 100},
	"binance_spot": {5, 10, 20, 50, 100, 500, 1000, 5000},
	"bitget_spot":  {1, 5, 15, 50, 100, 200},
	"mexc_spot":    {5, 10, 20, 50, 100, 500, 1000, 5000},
	"okx_spot":     {1, 5, 10, 20, 50, 100, 200, 400},
}

// Limit returns the smallest valid limit ≥ requested, or the requested value
// if the exchange has no canonical set (free-form depth).
func Limit(exchange string, requested int) int {
	valid, ok := validLimits[exchange]
	if !ok {
		return requested
	}
	for _, v := range valid {
		if v >= requested {
			return v
		}
	}
	// requested exceeds the largest accepted value — clamp to the cap.
	return valid[len(valid)-1]
}
