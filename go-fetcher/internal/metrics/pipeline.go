package metrics

import "time"

// Pre-registered metrics for the orderbook pipeline. cmd/fetcher/main.go
// is expected to wire these into cache.Store + the broadcast hooks at
// boot. Call sites use the typed helpers (Pipeline.RecordBookStore etc)
// so the metric names and label arities are pinned in one place.

var (
	booksStored = NewCounter(
		"avalant_book_store_total",
		"orderbook snapshots stored, per venue (post-Parse, pre-broadcast)",
		"venue", "source",
	)

	broadcastsOut = NewCounter(
		"avalant_broadcast_messages_total",
		"messages broadcast to WS clients, per channel",
		"channel",
	)

	lastUpdateAgeSec = NewGauge(
		"avalant_last_update_age_seconds",
		"wall-clock seconds since the last Store() update for the venue (0 if never)",
		"venue",
	)

	wsClientsOpen = NewGauge(
		"avalant_ws_clients_open",
		"open WebSocket clients connected to the broadcaster, per channel",
		"channel",
	)
)

// Pipeline groups the typed call helpers so they're easy to wire into
// existing call sites without leaking metric-name strings.
type Pipeline struct{}

func (Pipeline) RecordBookStore(venue, source string) {
	booksStored.Inc(venue, source)
}

func (Pipeline) RecordBroadcast(channel string) {
	broadcastsOut.Inc(channel)
}

func (Pipeline) SetClientsOpen(channel string, n int) {
	wsClientsOpen.Set(float64(n), channel)
}

// SetLastUpdateAge records the age, in seconds, since this venue's last
// store update. cmd/fetcher/main.go should poll cache.Store and call
// this periodically (e.g. every 5s) — no hot-path overhead.
func (Pipeline) SetLastUpdateAge(venue string, since time.Duration) {
	lastUpdateAgeSec.Set(since.Seconds(), venue)
}
