// spotshort.go — /ws/spot-short channel (Class 1 broadcaster).
//
// Mirrors longshort.go but reads spot_arbitrage.json (written by
// internal/arb/spot.go every 500ms). Wire format matches the existing
// REST endpoint /api/screener/spot-short so the frontend can switch
// transport without touching its row-rendering code:
//
//   First frame after connect (snapshot):
//     {"type":"snapshot","generated_at":..,"spot_exchanges":[..],
//      "opportunities":[...]}
//
//   Subsequent frames (diff at broadcastIntervalLongShort cadence):
//     {"type":"diff","generated_at":..,
//      "added":[...?], "updated":[...?], "removed":[[sym,spot_ex,short_ex],...?],
//      "spot_exchanges":[...?]}
//
// Cadence: same broadcastIntervalLongShort var as long-short — 2s when
// AVALANT_TIERED_FRESHNESS=1 (Class 1 aggregate), 100ms otherwise. The
// open-pair / alert tiers (Class 2/3) live in book.go — symbol-keyed
// hot-set already covers spot/short subscriptions transparently
// because the spot leg subscribes as `<ex>_spot:SYM` via the same
// /ws/book channel.
//
// Empty-snapshot guard: same 50%/5s rule the long-short side uses.
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

// SpotShort is the channel state. One instance owned by the Service.
type SpotShort struct {
	hub      *Hub
	cacheDir string

	mu               sync.Mutex
	lastByKey        map[string]map[string]any // key=sym|spot_ex|short_ex -> opp
	lastSpotExs      []any
	lastBroadcastAt  time.Time
	lastSnapshotJSON []byte
	lastFileMtime    int64
}

func NewSpotShort(cacheDir string) *SpotShort {
	return &SpotShort{
		hub:       NewHub("spot-short"),
		cacheDir:  cacheDir,
		lastByKey: make(map[string]map[string]any, 1024),
	}
}

func (s *SpotShort) Hub() *Hub { return s.hub }

// SnapshotForNewClient returns the most recent JSON-encoded snapshot.
// Cold start path: force-read the file ignoring mtime so the very first
// client gets a populated table before the broadcast loop's first tick.
func (s *SpotShort) SnapshotForNewClient() []byte {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.lastSnapshotJSON != nil {
		return s.lastSnapshotJSON
	}
	data := s.forceReadFile()
	if data == nil {
		return nil
	}
	snap := map[string]any{
		"type":           "snapshot",
		"generated_at":   data["generated_at"],
		"spot_exchanges": data["spot_exchanges"],
		"opportunities":  data["opportunities"],
	}
	b, _ := json.Marshal(snap)
	s.lastSnapshotJSON = b
	return b
}

// Run starts the broadcast loop.
func (s *SpotShort) Run(ctx context.Context) {
	t := time.NewTicker(broadcastIntervalLongShort)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			s.tick()
		}
	}
}

func (s *SpotShort) tick() {
	if s.hub.Count() == 0 {
		return // nobody to broadcast to — skip the file read entirely
	}
	data := s.readFile()
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
		k := spotKey(opp)
		if k == "" {
			continue
		}
		currByKey[k] = opp
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	// Empty-snapshot guard.
	prevCount := len(s.lastByKey)
	newCount := len(currByKey)
	now := time.Now()
	if prevCount > 0 && newCount < prevCount/2 && now.Sub(s.lastBroadcastAt) < 5*time.Second {
		log.L().Info().Int("prev", prevCount).Int("new", newCount).
			Dur("age", now.Sub(s.lastBroadcastAt)).Msg("spot-short empty-guard skip")
		return
	}

	added := []any{}
	updated := []any{}
	for k, opp := range currByKey {
		prev := s.lastByKey[k]
		if prev == nil {
			added = append(added, opp)
		} else if spotOppDiffers(prev, opp) {
			updated = append(updated, opp)
		}
	}
	removed := []any{}
	for k := range s.lastByKey {
		if _, ok := currByKey[k]; !ok {
			parts := splitSpotKey(k)
			removed = append(removed, parts)
		}
	}

	spotExsNow, _ := data["spot_exchanges"].([]any)
	spotExsChanged := !reflect.DeepEqual(spotExsNow, s.lastSpotExs)

	if len(added) == 0 && len(updated) == 0 && len(removed) == 0 && !spotExsChanged {
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
	if spotExsChanged {
		payload["spot_exchanges"] = spotExsNow
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return
	}
	s.hub.Broadcast(body)

	// Update state for next diff + refresh the snapshot cache.
	s.lastByKey = currByKey
	s.lastSpotExs = spotExsNow
	s.lastBroadcastAt = now

	snap := map[string]any{
		"type":           "snapshot",
		"generated_at":   data["generated_at"],
		"spot_exchanges": spotExsNow,
		"opportunities":  currOpps,
	}
	if snapBytes, err := json.Marshal(snap); err == nil {
		s.lastSnapshotJSON = snapBytes
	}
}

// readFile returns spot_arbitrage.json or nil on miss / unchanged mtime.
// MUST hold s.mu.
func (s *SpotShort) readFile() map[string]any {
	path := filepath.Join(s.cacheDir, "spot_arbitrage.json")
	info, err := os.Stat(path)
	if err != nil {
		return nil
	}
	mtime := info.ModTime().UnixNano()
	if mtime != 0 && mtime == s.lastFileMtime {
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
	s.lastFileMtime = mtime
	return doc
}

// forceReadFile bypasses mtime cache (cold-start client snapshot).
// MUST hold s.mu.
func (s *SpotShort) forceReadFile() map[string]any {
	path := filepath.Join(s.cacheDir, "spot_arbitrage.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var doc map[string]any
	if err := json.Unmarshal(raw, &doc); err != nil {
		return nil
	}
	if info, err := os.Stat(path); err == nil {
		s.lastFileMtime = info.ModTime().UnixNano()
	}
	return doc
}

// spotKey — row identifier: "<sym>|<spot_ex>|<short_ex>". Mirrors
// arbKey for L/S but uses `spot_exchange` instead of `long_exchange`
// since spot_arbitrage.json renames the long leg.
func spotKey(opp map[string]any) string {
	sym, _ := opp["symbol"].(string)
	se, _ := opp["spot_exchange"].(string)
	pe, _ := opp["short_exchange"].(string)
	if sym == "" || se == "" || pe == "" {
		return ""
	}
	return sym + "|" + se + "|" + pe
}

// splitSpotKey reverses spotKey → [sym, spot_ex, short_ex] as []any.
func splitSpotKey(k string) []any {
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

// spotOppDiffers — curated diff fields. Same idea as oppDiffers but
// for spot_arbitrage.json field names. Excludes generated_at-style
// fields that move every cycle but don't affect rendering.
func spotOppDiffers(a, b map[string]any) bool {
	keys := []string{
		"spot_price", "perp_price",
		"spot_volume_usd", "perp_volume_usd",
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
