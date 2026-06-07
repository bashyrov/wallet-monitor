// dexshort.go — /ws/dex-short channel (Class 1 broadcaster).
//
// Mirrors longshort.go but reads dex_arbitrage.json (written by
// internal/arb/dex.go every 30s; row count capped at top-200). Wire
// format matches the existing REST endpoint /api/screener/dex-short.
//
//   First frame after connect (snapshot):
//     {"type":"snapshot","generated_at":..,
//      "symbols_scanned":N,"dex_hits":M,"opportunities":[...]}
//
//   Subsequent frames (diff at broadcastIntervalLongShort cadence):
//     {"type":"diff","generated_at":..,
//      "added":[...?], "updated":[...?], "removed":[[sym,short_ex],...?],
//      "symbols_scanned":N?, "dex_hits":M?}
//
// Cadence: same broadcastIntervalLongShort as long-short. Note that
// dex_arbitrage.json itself is only rewritten every 30s (DexScreener
// rate-limit), so consecutive ticks within a 30s window see identical
// mtime → skip decode (free). Diff push happens on the rare ticks
// where mtime changed.
//
// IMPORTANT — what's NOT covered by tiering on dex/short (limitation,
// not bug):
//   - DEX-side price = DexScreener REST mid-price; NOT orderbook/WS.
//     There is no event-driven Class 2 path for the DEX leg of a
//     dex/short pair. Class 2 hot-bypass in book.go fires only for
//     the perp short leg (`<short_ex>:SYM`).
//   - This Class 1 broadcaster pushes the aggregate table; it does
//     not change the DEX leg's freshness ceiling, which is bounded
//     by the 30s arb compute cycle + DexScreener cache TTL.
package wsbroadcast

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"sync"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
)

// DexShort is the channel state.
type DexShort struct {
	hub      *Hub
	cacheDir string

	mu               sync.Mutex
	lastByKey        map[string]map[string]any // key=sym|short_ex -> opp
	lastSymbolsScanned any
	lastDexHits        any
	lastBroadcastAt    time.Time
	lastSnapshotJSON   []byte
	lastFileMtime      int64
}

func NewDexShort(cacheDir string) *DexShort {
	return &DexShort{
		hub:       NewHub("dex-short"),
		cacheDir:  cacheDir,
		lastByKey: make(map[string]map[string]any, 256),
	}
}

func (d *DexShort) Hub() *Hub { return d.hub }

// SnapshotForNewClient returns the most recent snapshot, force-reading
// the file on cold start.
func (d *DexShort) SnapshotForNewClient() []byte {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.lastSnapshotJSON != nil {
		return d.lastSnapshotJSON
	}
	data := d.forceReadFile()
	if data == nil {
		return nil
	}
	snap := map[string]any{
		"type":            "snapshot",
		"generated_at":    data["generated_at"],
		"symbols_scanned": data["symbols_scanned"],
		"dex_hits":        data["dex_hits"],
		"opportunities":   data["opportunities"],
	}
	b, _ := json.Marshal(snap)
	d.lastSnapshotJSON = b
	return b
}

// Run starts the broadcast loop.
func (d *DexShort) Run(ctx context.Context) {
	t := time.NewTicker(broadcastIntervalLongShort)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			d.tick()
		}
	}
}

func (d *DexShort) tick() {
	if d.hub.Count() == 0 {
		return
	}
	data := d.readFile()
	if data == nil {
		return
	}
	currOpps, _ := data["opportunities"].([]any)
	currByKey := make(map[string]map[string]any, len(currOpps))
	for _, o := range currOpps {
		opp, ok := o.(map[string]any)
		if !ok {
			continue
		}
		k := dexKey(opp)
		if k == "" {
			continue
		}
		currByKey[k] = opp
	}

	d.mu.Lock()
	defer d.mu.Unlock()

	// Empty-snapshot guard. Lower threshold relative to L/S — dex set
	// is much smaller (≤200), so 50% loss in a 5s window is a stronger
	// signal of a fetcher hiccup, but on cold start the same guard
	// allows the initial population through (prevCount=0).
	prevCount := len(d.lastByKey)
	newCount := len(currByKey)
	now := time.Now()
	if prevCount > 0 && newCount < prevCount/2 && now.Sub(d.lastBroadcastAt) < 5*time.Second {
		log.L().Info().Int("prev", prevCount).Int("new", newCount).
			Dur("age", now.Sub(d.lastBroadcastAt)).Msg("dex-short empty-guard skip")
		return
	}

	added := []any{}
	updated := []any{}
	for k, opp := range currByKey {
		prev := d.lastByKey[k]
		if prev == nil {
			added = append(added, opp)
		} else if dexOppDiffers(prev, opp) {
			updated = append(updated, opp)
		}
	}
	removed := []any{}
	for k := range d.lastByKey {
		if _, ok := currByKey[k]; !ok {
			parts := splitDexKey(k)
			removed = append(removed, parts)
		}
	}

	symsScannedNow := data["symbols_scanned"]
	dexHitsNow := data["dex_hits"]
	metaChanged := !reflect.DeepEqual(symsScannedNow, d.lastSymbolsScanned) ||
		!reflect.DeepEqual(dexHitsNow, d.lastDexHits)

	if len(added) == 0 && len(updated) == 0 && len(removed) == 0 && !metaChanged {
		return
	}

	payload := map[string]any{
		"type":         "diff",
		"generated_at": data["generated_at"],
	}
	if len(added) > 0 {
		payload["added"] = added
	}
	if len(updated) > 0 {
		payload["updated"] = updated
	}
	if len(removed) > 0 {
		payload["removed"] = removed
	}
	if metaChanged {
		payload["symbols_scanned"] = symsScannedNow
		payload["dex_hits"] = dexHitsNow
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return
	}
	d.hub.Broadcast(body)

	d.lastByKey = currByKey
	d.lastSymbolsScanned = symsScannedNow
	d.lastDexHits = dexHitsNow
	d.lastBroadcastAt = now

	snap := map[string]any{
		"type":            "snapshot",
		"generated_at":    data["generated_at"],
		"symbols_scanned": symsScannedNow,
		"dex_hits":        dexHitsNow,
		"opportunities":   currOpps,
	}
	if snapBytes, err := json.Marshal(snap); err == nil {
		d.lastSnapshotJSON = snapBytes
	}
}

// readFile returns dex_arbitrage.json or nil on miss / unchanged mtime.
// MUST hold d.mu.
func (d *DexShort) readFile() map[string]any {
	path := filepath.Join(d.cacheDir, "dex_arbitrage.json")
	info, err := os.Stat(path)
	if err != nil {
		return nil
	}
	mtime := info.ModTime().UnixNano()
	if mtime != 0 && mtime == d.lastFileMtime {
		return nil
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var doc map[string]any
	if err := json.Unmarshal(raw, &doc); err != nil {
		return nil
	}
	d.lastFileMtime = mtime
	return doc
}

func (d *DexShort) forceReadFile() map[string]any {
	path := filepath.Join(d.cacheDir, "dex_arbitrage.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var doc map[string]any
	if err := json.Unmarshal(raw, &doc); err != nil {
		return nil
	}
	if info, err := os.Stat(path); err == nil {
		d.lastFileMtime = info.ModTime().UnixNano()
	}
	return doc
}

// dexKey — row identifier: "<sym>|<short_ex>". DEX leg is canonical-
// per-symbol (pickFromPools chooses the top-liquidity pool per cycle)
// so symbol + short_ex is enough to uniquely key a row in the table.
// dex_chain/dex_name are values, not identity.
func dexKey(opp map[string]any) string {
	sym, _ := opp["symbol"].(string)
	pe, _ := opp["short_exchange"].(string)
	if sym == "" || pe == "" {
		return ""
	}
	return sym + "|" + pe
}

func splitDexKey(k string) []any {
	out := make([]any, 0, 2)
	start := 0
	for i := 0; i < len(k); i++ {
		if k[i] == '|' {
			out = append(out, k[start:i])
			start = i + 1
		}
	}
	out = append(out, k[start:])
	return out
}

// dexOppDiffers — fields that meaningfully change for the user.
// dex_chain/dex_name moves only on pool swap (rare); include them so
// the frontend updates its label, but skip dex_pair_url (cosmetic).
func dexOppDiffers(a, b map[string]any) bool {
	keys := []string{
		"dex_chain", "dex_name",
		"dex_price", "perp_price",
		"dex_liquidity_usd", "dex_volume_usd", "perp_volume_usd",
		"funding_rate", "short_funding_8h",
		"basis_pct", "gross", "net_profit", "net_apr",
		"in_pct", "out_pct",
	}
	for _, k := range keys {
		if !reflect.DeepEqual(a[k], b[k]) {
			return true
		}
	}
	return false
}
