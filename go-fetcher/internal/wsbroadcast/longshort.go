// longshort.go — /ws/long-short and /ws/arb (legacy alias) channel.
//
// Wire format matches the Python broadcaster exactly so the existing
// frontend can switch transport without code changes:
//
//   First frame after connect (snapshot):
//     {"type":"snapshot","ts":..,"fees":{...},"exchanges":[..],
//      "opportunities":[...]}
//
//   Subsequent frames (diff at BroadcastInterval cadence):
//     {"type":"diff","ts":..,
//      "added":[...?], "updated":[...?], "removed":[[sym,long,short],...?],
//      "fees":{...?}, "exchanges":[...?]}
//
// Empty-snapshot guard: if the latest arbitrage.json shows < 50 % of
// the previously-broadcast opp count AND the previous broadcast was
// recent (< 5 s), suppress the push to avoid the user seeing the table
// flash empty during a transient fetcher hiccup. Same rule the Python
// side uses.
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

// 250ms → 100ms — match the new arb-compute cadence (200ms). Clients
// merge diffs incrementally so 10×/sec push is fine. Reduces the
// user-visible "lag" between funding tick and screener row update.
const broadcastIntervalLongShortDefault = 100 * time.Millisecond

// broadcastIntervalLongShort is the actual push cadence for /ws/long-short
// (aka the screener table / watchlist tier — CLASS 1). When tiered
// freshness is on, this slows to 2s — the screener is an aggregate view
// where the user doesn't notice <2s latency on individual rows, and
// pushing every 100ms × 500 rows × N clients is wasted bandwidth. The
// open-pair / alert paths (CLASS 2 & 3) use the per-pair /ws/book
// channel with event-driven bypass, which is unaffected by this slowdown.
//
// Overridable per-deployment via AVALANT_LONGSHORT_BROADCAST_INTERVAL.
var broadcastIntervalLongShort = func() time.Duration {
	if v := os.Getenv("AVALANT_LONGSHORT_BROADCAST_INTERVAL"); v != "" {
		if d, err := time.ParseDuration(v); err == nil && d >= 50*time.Millisecond {
			return d
		}
	}
	if tieredFreshness {
		return 2 * time.Second
	}
	return broadcastIntervalLongShortDefault
}()

// LongShort is the channel state. One instance owned by the Service.
type LongShort struct {
	hub      *Hub
	cacheDir string

	mu               sync.Mutex
	lastByKey        map[string]map[string]any // key=sym|long|short -> opp
	lastFees         map[string]any
	lastExchanges    []any
	lastTS           any
	lastBroadcastAt  time.Time
	lastSnapshotJSON []byte // most recent full snapshot, sent to new clients
	lastFileMtime    int64  // ns precision; ticks where mtime unchanged skip decode entirely
}

func NewLongShort(cacheDir string) *LongShort {
	return &LongShort{
		hub:       NewHub("long-short"),
		cacheDir:  cacheDir,
		lastByKey: make(map[string]map[string]any, 1024),
	}
}

func (l *LongShort) Hub() *Hub { return l.hub }

// SnapshotForNewClient returns the most recent JSON-encoded snapshot.
// New clients receive this immediately after handshake to populate
// their initial table.
func (l *LongShort) SnapshotForNewClient() []byte {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.lastSnapshotJSON != nil {
		return l.lastSnapshotJSON
	}
	// Cold start — pre-tick. Force-read the file (ignoring mtime cache)
	// so the very first connecting client gets a populated table.
	data := l.forceReadArbFile()
	if data == nil {
		return nil
	}
	snap := map[string]any{
		"type":          "snapshot",
		"ts":            data["ts"],
		"fees":          data["fees"],
		"exchanges":     data["exchanges"],
		"opportunities": data["opportunities"],
	}
	if v, ok := data["truncated_to"]; ok {
		snap["truncated_to"] = v
	}
	if v, ok := data["full_count"]; ok {
		snap["full_count"] = v
	}
	b, _ := json.Marshal(snap)
	// Cache so subsequent cold-start clients don't re-decode the file
	// before the first tick has run.
	l.lastSnapshotJSON = b
	return b
}

// Run starts the broadcast loop. Reads arbitrage.json every
// broadcastIntervalLongShort, builds a diff, pushes to all hub clients.
func (l *LongShort) Run(ctx context.Context) {
	t := time.NewTicker(broadcastIntervalLongShort)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			l.tick()
		}
	}
}

func (l *LongShort) tick() {
	if l.hub.Count() == 0 {
		return // nobody to broadcast to — skip the file read entirely
	}
	data := l.readArbFile()
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
		k := arbKey(opp)
		if k == "" {
			continue
		}
		currByKey[k] = opp
	}

	l.mu.Lock()
	defer l.mu.Unlock()

	// Empty-snapshot guard: same rule Python uses.
	prevCount := len(l.lastByKey)
	newCount := len(currByKey)
	now := time.Now()
	if prevCount > 0 && newCount < prevCount/2 && now.Sub(l.lastBroadcastAt) < 5*time.Second {
		log.L().Info().Int("prev", prevCount).Int("new", newCount).
			Dur("age", now.Sub(l.lastBroadcastAt)).Msg("long-short empty-guard skip")
		return
	}

	added := []any{}
	updated := []any{}
	for k, opp := range currByKey {
		prev := l.lastByKey[k]
		if prev == nil {
			added = append(added, opp)
		} else if oppDiffers(prev, opp) {
			updated = append(updated, opp)
		}
	}
	removed := []any{}
	for k := range l.lastByKey {
		if _, ok := currByKey[k]; !ok {
			parts := splitArbKey(k)
			removed = append(removed, parts)
		}
	}

	feesNow, _ := data["fees"].(map[string]any)
	exchangesNow, _ := data["exchanges"].([]any)
	feesChanged := !reflect.DeepEqual(feesNow, l.lastFees)
	exchChanged := !reflect.DeepEqual(exchangesNow, l.lastExchanges)
	metaChanged := feesChanged || exchChanged

	if len(added) == 0 && len(updated) == 0 && len(removed) == 0 && !metaChanged {
		return
	}

	payload := map[string]any{
		"type": "diff",
		"ts":   data["ts"],
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
		payload["fees"] = feesNow
		payload["exchanges"] = exchangesNow
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return
	}
	l.hub.Broadcast(body)

	// Update state for next diff + cache the snapshot for new clients.
	l.lastByKey = currByKey
	l.lastFees = feesNow
	l.lastExchanges = exchangesNow
	l.lastTS = data["ts"]
	l.lastBroadcastAt = now

	snap := map[string]any{
		"type":          "snapshot",
		"ts":            data["ts"],
		"fees":          feesNow,
		"exchanges":     exchangesNow,
		"opportunities": currOpps,
	}
	if v, ok := data["truncated_to"]; ok {
		snap["truncated_to"] = v
	}
	if v, ok := data["full_count"]; ok {
		snap["full_count"] = v
	}
	if snapBytes, err := json.Marshal(snap); err == nil {
		l.lastSnapshotJSON = snapBytes
	}
}

// readArbFile returns the latest arbitrage.json contents or nil if
// missing/corrupt OR unchanged since the previous read (mtime-skip).
//
// The arb compute writes the file every 500 ms but the broadcaster
// ticks every 100 ms — 4 of every 5 ticks see the same file. Stat'ing
// before decode short-circuits the wasted JSON parse (the file is
// 200-500 KB of map[string]any — non-trivial unmarshal cost).
//
// MUST hold l.mu when calling.
func (l *LongShort) readArbFile() map[string]any {
	path := filepath.Join(l.cacheDir, "arbitrage.json")
	info, err := os.Stat(path)
	if err != nil {
		return nil
	}
	mtime := info.ModTime().UnixNano()
	if mtime != 0 && mtime == l.lastFileMtime {
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
	l.lastFileMtime = mtime
	return doc
}

// forceReadArbFile bypasses the mtime cache. Used by SnapshotForNewClient
// on cold start when no tick has yet populated lastSnapshotJSON — we want
// the first connecting client to see whatever's currently on disk, even
// if a prior call established the mtime baseline.
//
// MUST hold l.mu when calling.
func (l *LongShort) forceReadArbFile() map[string]any {
	path := filepath.Join(l.cacheDir, "arbitrage.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var doc map[string]any
	if err := json.Unmarshal(raw, &doc); err != nil {
		return nil
	}
	if info, err := os.Stat(path); err == nil {
		l.lastFileMtime = info.ModTime().UnixNano()
	}
	return doc
}

// arbKey — same shape as Python's _arb_key: "<sym>|<long_ex>|<short_ex>".
func arbKey(opp map[string]any) string {
	sym, _ := opp["symbol"].(string)
	le, _ := opp["long_exchange"].(string)
	se, _ := opp["short_exchange"].(string)
	if sym == "" || le == "" || se == "" {
		return ""
	}
	return sym + "|" + le + "|" + se
}

// splitArbKey reverses arbKey. Returned as []any to match Python's
// `removed: [["BTC","binance","bybit"], ...]` shape.
func splitArbKey(k string) []any {
	out := make([]any, 0, 3)
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

// oppDiffers returns true when two opportunity records meaningfully
// differ for the user's view. We compare a curated subset rather than
// reflect.DeepEqual'ing the whole map — values like ts and last-update
// move every cycle but mean nothing to the renderer.
func oppDiffers(a, b map[string]any) bool {
	keys := []string{
		"long_rate", "short_rate", "long_price", "short_price",
		"long_volume", "short_volume",
		"gross_funding", "price_spread", "net_profit", "net_apr",
		"in_pct", "out_pct",
		"valid_price",
	}
	for _, k := range keys {
		if !reflect.DeepEqual(a[k], b[k]) {
			return true
		}
	}
	return false
}
