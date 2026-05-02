package trade

import (
	"strings"
	"sync"
)

// Registry holds the global name→Adapter mapping. Adapters self-register
// in their package init() so main.go never has to import them
// individually — keeps the wiring tidy as we add venues.
//
//	import _ "github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade/binance"
//	// adapter.binance is now in trade.Lookup("binance").
//
// Registration is idempotent — a second Register() with the same name
// replaces the first. That's intentional so a test can swap an adapter
// for a fake without restructuring imports.
var (
	mu        sync.RWMutex
	registry  = map[string]Adapter{}
)

// Register adds (or replaces) an adapter under the given name. Name is
// lower-cased to match what `Wallet.type_value` stores in DB.
func Register(name string, adapter Adapter) {
	if adapter == nil {
		panic("trade.Register: nil adapter for " + name)
	}
	mu.Lock()
	registry[strings.ToLower(name)] = adapter
	mu.Unlock()
}

// Lookup — returns the adapter for `name` or nil if no Go adapter is
// registered yet (Python falls through for that venue).
func Lookup(name string) Adapter {
	mu.RLock()
	a := registry[strings.ToLower(name)]
	mu.RUnlock()
	return a
}

// SupportedExchanges returns the list of exchanges Go can handle right
// now. Used by the HTTP layer to advertise its capabilities so the
// Python proxy can decide per-call whether to dispatch over to Go.
func SupportedExchanges() []string {
	mu.RLock()
	defer mu.RUnlock()
	out := make([]string, 0, len(registry))
	for k := range registry {
		out = append(out, k)
	}
	return out
}
