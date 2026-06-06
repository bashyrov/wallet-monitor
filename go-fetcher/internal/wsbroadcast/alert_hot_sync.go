// alert_hot_sync.go — CLASS 3 wiring.
//
// Reads active_alerts.json (written by Python alert_service every 10s)
// and replays the symbol list into Book.ReplaceAlertHot so /ws/book's
// event-driven bypass-pending path kicks in for those symbols even when
// no client is currently on /arb for them.
//
// mtime cache: skip the json.Unmarshal entirely if the file hasn't
// changed since the last poll. With ~0-10 active alerts on a normal day
// this loop costs <1us per tick when idle.
package wsbroadcast

import (
	"context"
	"encoding/json"
	"os"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
)

type activeAlertsFile struct {
	Symbols []string `json:"symbols"`
	TS      int64    `json:"ts"`
}

// RunAlertHotSync polls path every interval and pushes the union of
// symbols into book.ReplaceAlertHot. Returns when ctx is cancelled.
// Safe to call when AVALANT_TIERED_FRESHNESS=0 — ReplaceAlertHot no-ops.
func RunAlertHotSync(ctx context.Context, book *Book, path string, interval time.Duration) {
	if book == nil {
		return
	}
	if interval <= 0 {
		interval = 10 * time.Second
	}
	t := time.NewTicker(interval)
	defer t.Stop()
	var lastMtime int64
	// Immediate read on startup so we don't wait the first `interval` to
	// apply the current alert set.
	if applyOnce(book, path, &lastMtime) {
		log.L().Info().Str("path", path).Msg("alert hot-set sync started")
	}
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			applyOnce(book, path, &lastMtime)
		}
	}
}

func applyOnce(book *Book, path string, lastMtime *int64) bool {
	info, err := os.Stat(path)
	if err != nil {
		// Missing file is normal on cold start; ignore.
		return false
	}
	mt := info.ModTime().UnixNano()
	if mt == *lastMtime {
		return false
	}
	*lastMtime = mt
	raw, err := os.ReadFile(path)
	if err != nil {
		log.L().Debug().Err(err).Str("path", path).Msg("alert hot-set read failed")
		return false
	}
	var doc activeAlertsFile
	if err := json.Unmarshal(raw, &doc); err != nil {
		log.L().Debug().Err(err).Msg("alert hot-set decode failed")
		return false
	}
	book.ReplaceAlertHot(doc.Symbols)
	log.L().Debug().Int("symbols", len(doc.Symbols)).Msg("alert hot-set applied")
	return true
}
