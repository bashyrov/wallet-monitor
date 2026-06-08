// dexspot.go — /ws/dex-spot channel (Class 1 broadcaster).
//
// Mirrors dexshort.go but reads dex_spot_arbitrage.json (DEX↔CEX spot
// arb, written by internal/arb/dex_spot.go every 30s when
// AVALANT_DEX_SPOT=1). Wire format matches the parallel REST endpoint
// /api/screener/dex-spot.
//
//   First frame after connect (snapshot):
//     {"type":"snapshot","generated_at":..,
//      "symbols_scanned":N,"cex_hits":M,"cex_exchanges":[..],
//      "opportunities":[...]}
//
//   Subsequent frames (diff at broadcastIntervalLongShort cadence):
//     {"type":"diff","generated_at":..,
//      "added":[...?], "updated":[...?], "removed":[[sym,cex_ex],...?],
//      "symbols_scanned":N?, "cex_hits":M?, "cex_exchanges":[...?]}
//
// Same cadence + empty-snapshot guard rules dexshort.go uses. Row key
// is "<sym>|<cex_exchange>" — DEX details (chain, dex_name) are part of
// the row value, not identity, because pickFromPools picks one canonical
// pool per symbol.
//
// IMPORTANT — limits inherited from data sources (NOT bugs):
//   - DEX-side price is DexScreener REST mid (30s cycle). Event-driven
//     Class 2 is impossible on the DEX leg. The CEX-spot leg HAS its
//     own spot WS adapter ('<ex>_spot:SYM') already on /ws/book, so
//     opened-pair tiering works for that side.
//   - dex_spot_arbitrage.json itself is only rewritten every 30s, so
//     this broadcaster's tick mostly sees unchanged mtime → skip
//     decode (same free-when-quiet behaviour dexshort has).
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

// DexSpot is the channel state.
type DexSpot struct {
	hub      *Hub
	cacheDir string

	mu                 sync.Mutex
	lastByKey          map[string]map[string]any // key=sym|cex_ex -> opp
	lastSymbolsScanned any
	lastCexHits        any
	lastCexExchanges   []any
	lastBroadcastAt    time.Time
	lastSnapshotJSON   []byte
	lastFileMtime      int64
}

func NewDexSpot(cacheDir string) *DexSpot {
	return &DexSpot{
		hub:       NewHub("dex-spot"),
		cacheDir:  cacheDir,
		lastByKey: make(map[string]map[string]any, 256),
	}
}

func (d *DexSpot) Hub() *Hub { return d.hub }

// SnapshotForNewClient returns the most recent snapshot, force-reading
// the file on cold start.
func (d *DexSpot) SnapshotForNewClient() []byte {
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
		"cex_hits":        data["cex_hits"],
		"cex_exchanges":   data["cex_exchanges"],
		"opportunities":   data["opportunities"],
	}
	b, _ := json.Marshal(snap)
	d.lastSnapshotJSON = b
	return b
}

// Run starts the broadcast loop.
func (d *DexSpot) Run(ctx context.Context) {
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

func (d *DexSpot) tick() {
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
		k := dexSpotKey(opp)
		if k == "" {
			continue
		}
		currByKey[k] = opp
	}

	d.mu.Lock()
	defer d.mu.Unlock()

	// Empty-snapshot guard (same as dexshort).
	prevCount := len(d.lastByKey)
	newCount := len(currByKey)
	now := time.Now()
	if prevCount > 0 && newCount < prevCount/2 && now.Sub(d.lastBroadcastAt) < 5*time.Second {
		log.L().Info().Int("prev", prevCount).Int("new", newCount).
			Dur("age", now.Sub(d.lastBroadcastAt)).Msg("dex-spot empty-guard skip")
		return
	}

	added := []any{}
	updated := []any{}
	for k, opp := range currByKey {
		prev := d.lastByKey[k]
		if prev == nil {
			added = append(added, opp)
		} else if dexSpotOppDiffers(prev, opp) {
			updated = append(updated, opp)
		}
	}
	removed := []any{}
	for k := range d.lastByKey {
		if _, ok := currByKey[k]; !ok {
			parts := splitDexSpotKey(k)
			removed = append(removed, parts)
		}
	}

	symsScannedNow := data["symbols_scanned"]
	cexHitsNow := data["cex_hits"]
	cexExNow, _ := data["cex_exchanges"].([]any)
	metaChanged := !reflect.DeepEqual(symsScannedNow, d.lastSymbolsScanned) ||
		!reflect.DeepEqual(cexHitsNow, d.lastCexHits) ||
		!reflect.DeepEqual(cexExNow, d.lastCexExchanges)

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
		payload["cex_hits"] = cexHitsNow
		payload["cex_exchanges"] = cexExNow
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return
	}
	d.hub.Broadcast(body)

	d.lastByKey = currByKey
	d.lastSymbolsScanned = symsScannedNow
	d.lastCexHits = cexHitsNow
	d.lastCexExchanges = cexExNow
	d.lastBroadcastAt = now

	snap := map[string]any{
		"type":            "snapshot",
		"generated_at":    data["generated_at"],
		"symbols_scanned": symsScannedNow,
		"cex_hits":        cexHitsNow,
		"cex_exchanges":   cexExNow,
		"opportunities":   currOpps,
	}
	if snapBytes, err := json.Marshal(snap); err == nil {
		d.lastSnapshotJSON = snapBytes
	}
}

// readFile returns dex_spot_arbitrage.json or nil on miss / unchanged mtime.
// MUST hold d.mu.
func (d *DexSpot) readFile() map[string]any {
	path := filepath.Join(d.cacheDir, "dex_spot_arbitrage.json")
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

func (d *DexSpot) forceReadFile() map[string]any {
	path := filepath.Join(d.cacheDir, "dex_spot_arbitrage.json")
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

// dexSpotKey — row identifier: "<sym>|<cex_exchange>". DEX details
// are values per the canonical-pool-per-symbol invariant.
func dexSpotKey(opp map[string]any) string {
	sym, _ := opp["symbol"].(string)
	ce, _ := opp["cex_exchange"].(string)
	if sym == "" || ce == "" {
		return ""
	}
	return sym + "|" + ce
}

func splitDexSpotKey(k string) []any {
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

// dexSpotOppDiffers — fields that matter for the user's view. direction
// flips on price-cross; dex_chain/dex_name on pool swap (rare). Skip
// cosmetic fields like dex_pair_url + dex_base_address.
func dexSpotOppDiffers(a, b map[string]any) bool {
	keys := []string{
		"direction", "dex_chain", "dex_name",
		"dex_price", "cex_spot_price",
		"dex_liquidity_usd", "dex_volume_usd", "cex_volume_usd",
		"spread_pct", "abs_spread_pct", "net_pct",
	}
	for _, k := range keys {
		if !reflect.DeepEqual(a[k], b[k]) {
			return true
		}
	}
	return false
}
