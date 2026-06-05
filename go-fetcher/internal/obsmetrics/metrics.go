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
)

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
	})
}
