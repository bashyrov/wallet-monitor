// funding.go — /ws/funding channel.
//
// Wire format mirrors the Python broadcaster exactly so the existing
// frontend can read snapshot+diff frames without any code changes:
//
//   First frame (snapshot):
//     {"type":"snapshot","ts":..,"rows":[...],"exchanges":[..]}
//
//   Subsequent frames (diff at broadcastIntervalFunding cadence):
//     {"type":"diff","ts":..,
//      "added":[...?], "updated":[...?], "removed":[[ex,sym],...?]}
//
// Empty-snapshot guard: same shape as long-short, but with the
// >100-rows minimum Python uses (matches `_build_funding_diff`). A
// transient drop to <50 % of the previous count is suppressed so
// users don't see the table flash empty during a fetcher hiccup.
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

// 100ms — funding rates change every 8h, but mark-price + 24h volume
// + interval-to-next-funding update continuously. 10×/sec broadcast
// keeps countdown timers and prices buttery-smooth. Dedicated goroutine
// so cost is essentially free.
const broadcastIntervalFunding = 100 * time.Millisecond

type Funding struct {
	hub      *Hub
	cacheDir string

	mu               sync.Mutex
	lastByKey        map[string]map[string]any // key=ex|sym -> row
	lastExchanges    []any
	lastBroadcastAt  time.Time
	lastSnapshotJSON []byte
}

func NewFunding(cacheDir string) *Funding {
	return &Funding{
		hub:       NewHub("funding"),
		cacheDir:  cacheDir,
		lastByKey: make(map[string]map[string]any, 2048),
	}
}

func (f *Funding) Hub() *Hub { return f.hub }

func (f *Funding) SnapshotForNewClient() []byte {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.lastSnapshotJSON != nil {
		return f.lastSnapshotJSON
	}
	data := f.readFundingFile()
	if data == nil {
		return nil
	}
	snap := map[string]any{
		"type":      "snapshot",
		"ts":        data["ts"],
		"rows":      data["rows"],
		"exchanges": data["exchanges"],
	}
	b, _ := json.Marshal(snap)
	return b
}

func (f *Funding) Run(ctx context.Context) {
	t := time.NewTicker(broadcastIntervalFunding)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			f.tick()
		}
	}
}

func (f *Funding) tick() {
	if f.hub.Count() == 0 {
		return
	}
	data := f.readFundingFile()
	if data == nil {
		return
	}
	currRows, _ := data["rows"].([]any)
	currByKey := make(map[string]map[string]any, len(currRows))
	for _, r := range currRows {
		row, ok := r.(map[string]any)
		if !ok {
			continue
		}
		k := fundingKey(row)
		if k == "" {
			continue
		}
		currByKey[k] = row
	}

	f.mu.Lock()
	defer f.mu.Unlock()

	// Empty-guard: Python uses prev_count > 100 here (not the > 0
	// threshold long-short uses) because funding has many more rows
	// per cycle and a one-tick gap is noisier.
	prevCount := len(f.lastByKey)
	newCount := len(currByKey)
	if prevCount > 100 && newCount < prevCount/2 {
		log.L().Info().Int("prev", prevCount).Int("new", newCount).
			Msg("funding empty-guard skip")
		return
	}

	added := []any{}
	updated := []any{}
	for k, row := range currByKey {
		prev := f.lastByKey[k]
		if prev == nil {
			added = append(added, row)
		} else if fundingDiffers(prev, row) {
			updated = append(updated, row)
		}
	}
	removed := []any{}
	for k := range f.lastByKey {
		if _, ok := currByKey[k]; !ok {
			removed = append(removed, splitFundingKey(k))
		}
	}

	if len(added) == 0 && len(updated) == 0 && len(removed) == 0 {
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
	body, err := json.Marshal(payload)
	if err != nil {
		return
	}
	f.hub.Broadcast(body)

	exchangesNow, _ := data["exchanges"].([]any)
	f.lastByKey = currByKey
	f.lastExchanges = exchangesNow
	f.lastBroadcastAt = time.Now()
	_ = reflect.DeepEqual // keep import for future meta-diff if needed

	snap := map[string]any{
		"type":      "snapshot",
		"ts":        data["ts"],
		"rows":      currRows,
		"exchanges": exchangesNow,
	}
	if snapBytes, err := json.Marshal(snap); err == nil {
		f.lastSnapshotJSON = snapBytes
	}
}

func (f *Funding) readFundingFile() map[string]any {
	path := filepath.Join(f.cacheDir, "funding.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var doc map[string]any
	if err := json.Unmarshal(raw, &doc); err != nil {
		return nil
	}
	return doc
}

// fundingKey mirrors Python's _funding_key: (exchange, symbol). Order
// matches the `removed` shape Python emits — `[ex, sym]`.
func fundingKey(r map[string]any) string {
	ex, _ := r["exchange"].(string)
	sym, _ := r["symbol"].(string)
	if ex == "" || sym == "" {
		return ""
	}
	return ex + "|" + sym
}

// splitFundingKey reverses fundingKey. Returned as []any so the JSON
// encoder emits a 2-element array (Python returns `list(k)`).
func splitFundingKey(k string) []any {
	for i := 0; i < len(k); i++ {
		if k[i] == '|' {
			return []any{k[:i], k[i+1:]}
		}
	}
	return []any{k}
}

// fundingDiffers — same field set Python's _FUNDING_DIFF_FIELDS uses.
// Anything outside this set (cross_listed, etc.) is either a derived
// flag or doesn't change tick-to-tick, so don't trigger an update on it.
func fundingDiffers(a, b map[string]any) bool {
	keys := []string{"rate", "price", "volume_usd", "next_ts", "interval_h", "apr"}
	for _, k := range keys {
		if !reflect.DeepEqual(a[k], b[k]) {
			return true
		}
	}
	return false
}
