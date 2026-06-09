// Package obsmetrics — lightweight per-exchange Prometheus-format metrics
// for the orderbook pipeline.
//
// No prometheus/client_golang dependency: metrics are hand-rolled atomic
// uint64 counters exposed via HTTP in the standard Prometheus text format.
// Add the go-fetcher host to your Prometheus scrape config:
//
//	scrape_configs:
//	  - job_name: avalant_fetcher
//	    static_configs:
//	      - targets: ['go-fetcher:8090']
//	    metrics_path: /internal/metrics
//
// Metrics exposed:
//
//	avalant_ob_updates_total{exchange}    — orderbook update frames processed
//	avalant_ob_reconnects_total{exchange} — WS session reconnects initiated
//	avalant_ob_resyncs_total{exchange}    — seq-gap resyncs (ErrResync fired)
package obsmetrics

import (
	"fmt"
	"net/http"
	"sort"
	"sync"
	"sync/atomic"
)

// counterVec is a labeled counter backed by sync.Map (lock-free fast path).
type counterVec struct {
	name string
	help string
	m    sync.Map // label → *uint64
}

func newCounterVec(name, help string) *counterVec {
	return &counterVec{name: name, help: help}
}

// Inc increments the counter for the given label by 1.
func (c *counterVec) Inc(label string) {
	v, _ := c.m.LoadOrStore(label, new(uint64))
	atomic.AddUint64(v.(*uint64), 1)
}

// snapshot returns a stable copy of label→value for serialisation.
func (c *counterVec) snapshot() map[string]uint64 {
	out := make(map[string]uint64)
	c.m.Range(func(k, v any) bool {
		out[k.(string)] = atomic.LoadUint64(v.(*uint64))
		return true
	})
	return out
}

// Global counters — zero-allocation increment on the hot path.
var (
	Updates    = newCounterVec("avalant_ob_updates_total", "Total orderbook update frames processed by the runner")
	Reconnects = newCounterVec("avalant_ob_reconnects_total", "Total WS session reconnects initiated (transient + policy)")
	Resyncs    = newCounterVec("avalant_ob_resyncs_total", "Total seq-gap resyncs (ErrResync returned by Parse)")

	// Tiered freshness model (AVALANT_TIERED_FRESHNESS=1)
	//   BookBypassPushes — count of OnBookUpdate frames pushed direct to
	//     subscribers, bypassing the 50ms pending-flush buffer. Labelled
	//     by "ex:SYM" pair. Compare with avalant_ob_updates_total to see
	//     the hot-set bypass rate.
	//   BookHotFloorDrops — count of OnBookUpdate frames that hit the
	//     hotPairFloor (10ms per-pair gap) and fell through to the
	//     pending buffer instead. Should be ~0 on cold pairs, low single
	//     digits per second on the hottest BTC during volatile moments.
	BookBypassPushes  = newCounterVec("avalant_book_bypass_pushes_total", "Book frames sent event-driven (bypassing flush) — pair label")
	BookHotFloorDrops = newCounterVec("avalant_book_hot_floor_drops_total", "OnBookUpdate frames coalesced because they hit the 10ms hot floor")

	// AdapterChanFramesIn — recv-side counter PER CHANNEL on multi-channel
	// adapters (OKX: books + bbo-tbt; Binance: bookTicker + depth; ...).
	// Lets us prove input rate per channel separately, so a venue's
	// real-time channel "going silent" gets caught before being blamed on
	// market quietness. Label format: "<exchange>/<channel>:<SYM>".
	AdapterChanFramesIn = newCounterVec("avalant_adapter_chan_frames_in_total", "WS frames received from the venue, labelled by adapter/channel/symbol")
)

// HotSetSize is a single-value gauge. Set by Book.report() each time the
// hot set changes. Read-only via Get(); /internal/metrics serialises this
// alongside the counter vecs.
var hotSetSize uint64

func SetHotSetSize(n int) {
	if n < 0 {
		n = 0
	}
	atomic.StoreUint64(&hotSetSize, uint64(n))
}

func GetHotSetSize() uint64 { return atomic.LoadUint64(&hotSetSize) }

// Handler returns an http.Handler that emits all counters in Prometheus text
// format (exposition format 0.0.4, text version 0.0.1).
func Handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
		for _, cv := range []*counterVec{Updates, Reconnects, Resyncs} {
			fmt.Fprintf(w, "# HELP %s %s\n", cv.name, cv.help)
			fmt.Fprintf(w, "# TYPE %s counter\n", cv.name)
			snap := cv.snapshot()
			labels := make([]string, 0, len(snap))
			for l := range snap {
				labels = append(labels, l)
			}
			sort.Strings(labels)
			for _, l := range labels {
				fmt.Fprintf(w, "%s{exchange=%q} %d\n", cv.name, l, snap[l])
			}
		}
		// Tiered freshness counters use pair labels (ex:SYM) rather than
		// exchange-only. Different cardinality bucket — render separately.
		for _, cv := range []*counterVec{BookBypassPushes, BookHotFloorDrops, AdapterChanFramesIn} {
			fmt.Fprintf(w, "# HELP %s %s\n", cv.name, cv.help)
			fmt.Fprintf(w, "# TYPE %s counter\n", cv.name)
			snap := cv.snapshot()
			labels := make([]string, 0, len(snap))
			for l := range snap {
				labels = append(labels, l)
			}
			sort.Strings(labels)
			for _, l := range labels {
				fmt.Fprintf(w, "%s{pair=%q} %d\n", cv.name, l, snap[l])
			}
		}
		fmt.Fprintf(w, "# HELP avalant_book_hot_set_size Current size of the /ws/book hot symbol set (Class 2 ∪ Class 3)\n")
		fmt.Fprintf(w, "# TYPE avalant_book_hot_set_size gauge\n")
		fmt.Fprintf(w, "avalant_book_hot_set_size %d\n", GetHotSetSize())
	})
}
