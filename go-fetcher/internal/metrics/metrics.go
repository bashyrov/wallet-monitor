// Package metrics — lightweight in-process counters + gauges with a
// Prometheus-text exporter for the /metrics HTTP endpoint.
//
// Designed for ONE process (go-fetcher) — no remote write, no histogram
// buckets, no cardinality safety. We just need the operator to be able
// to verify the data plane is alive + measure per-venue throughput.
//
// Scope:
//   - Counter — monotonic int64, +1 (or +N) per event, labelled.
//   - Gauge   — settable float64, labelled.
//
// Cardinality contract: callers are expected to pre-allocate label sets
// (e.g. one venue per Counter.WithVenue("binance")). Each unique label
// value spawns a series; that's fine for ~20 venues × ~5 metrics but
// would blow up if mis-used for per-symbol labels.
package metrics

import (
	"math"
	"sort"
	"sync"
	"sync/atomic"
)

// Default global registry. cmd/fetcher/main.go can call metrics.HTTPHandler()
// to mount the /metrics endpoint without passing the registry around.
var Default = NewRegistry()

// Registry holds named metric collectors. NOT a map of bare series —
// the caller registers one collector per metric name, then drives label
// values through With*().
type Registry struct {
	mu         sync.RWMutex
	counters   map[string]*Counter
	gauges     map[string]*Gauge
}

func NewRegistry() *Registry {
	return &Registry{
		counters: make(map[string]*Counter),
		gauges:   make(map[string]*Gauge),
	}
}

// Counter is a monotonic int64 split per label set. Label keys are
// fixed at registration time (NewCounter("name", "help", "venue")).
type Counter struct {
	name  string
	help  string
	keys  []string
	mu    sync.RWMutex
	vals  map[string]*int64 // label-value-tuple → atomic int64
}

// Gauge is a settable float64 split per label set.
type Gauge struct {
	name  string
	help  string
	keys  []string
	mu    sync.RWMutex
	vals  map[string]*atomicFloat
}

// NewCounter registers (or returns the existing) Counter on Default.
func NewCounter(name, help string, labelKeys ...string) *Counter {
	return Default.NewCounter(name, help, labelKeys...)
}

func (r *Registry) NewCounter(name, help string, labelKeys ...string) *Counter {
	r.mu.Lock()
	defer r.mu.Unlock()
	if c, ok := r.counters[name]; ok {
		return c
	}
	c := &Counter{name: name, help: help, keys: append([]string(nil), labelKeys...), vals: make(map[string]*int64)}
	r.counters[name] = c
	return c
}

// NewGauge registers (or returns the existing) Gauge on Default.
func NewGauge(name, help string, labelKeys ...string) *Gauge {
	return Default.NewGauge(name, help, labelKeys...)
}

func (r *Registry) NewGauge(name, help string, labelKeys ...string) *Gauge {
	r.mu.Lock()
	defer r.mu.Unlock()
	if g, ok := r.gauges[name]; ok {
		return g
	}
	g := &Gauge{name: name, help: help, keys: append([]string(nil), labelKeys...), vals: make(map[string]*atomicFloat)}
	r.gauges[name] = g
	return g
}

// Add atomically bumps the counter for the matching label values.
// labelValues must have the same arity as the registered labelKeys.
func (c *Counter) Add(delta int64, labelValues ...string) {
	if len(labelValues) != len(c.keys) {
		return // mis-arity is a programming error; silently drop in prod
	}
	key := joinLabels(labelValues)
	c.mu.RLock()
	p, ok := c.vals[key]
	c.mu.RUnlock()
	if !ok {
		c.mu.Lock()
		p, ok = c.vals[key]
		if !ok {
			p = new(int64)
			c.vals[key] = p
		}
		c.mu.Unlock()
	}
	atomic.AddInt64(p, delta)
}

// Inc is shorthand for Add(1, ...).
func (c *Counter) Inc(labelValues ...string) { c.Add(1, labelValues...) }

// Set atomically writes the gauge value for the matching label values.
func (g *Gauge) Set(v float64, labelValues ...string) {
	if len(labelValues) != len(g.keys) {
		return
	}
	key := joinLabels(labelValues)
	g.mu.RLock()
	p, ok := g.vals[key]
	g.mu.RUnlock()
	if !ok {
		g.mu.Lock()
		p, ok = g.vals[key]
		if !ok {
			p = &atomicFloat{}
			g.vals[key] = p
		}
		g.mu.Unlock()
	}
	p.Store(v)
}

// snapshotCounters returns (name, help, keys, perLabelTuple→count) copies.
func (r *Registry) snapshotCounters() []counterDump {
	r.mu.RLock()
	out := make([]counterDump, 0, len(r.counters))
	names := make([]string, 0, len(r.counters))
	for n := range r.counters {
		names = append(names, n)
	}
	sort.Strings(names)
	for _, n := range names {
		c := r.counters[n]
		c.mu.RLock()
		entries := make([]labeledValue, 0, len(c.vals))
		for k, p := range c.vals {
			entries = append(entries, labeledValue{k, float64(atomic.LoadInt64(p))})
		}
		c.mu.RUnlock()
		sort.Slice(entries, func(i, j int) bool { return entries[i].key < entries[j].key })
		out = append(out, counterDump{
			name:    c.name,
			help:    c.help,
			keys:    c.keys,
			entries: entries,
		})
	}
	r.mu.RUnlock()
	return out
}

func (r *Registry) snapshotGauges() []gaugeDump {
	r.mu.RLock()
	out := make([]gaugeDump, 0, len(r.gauges))
	names := make([]string, 0, len(r.gauges))
	for n := range r.gauges {
		names = append(names, n)
	}
	sort.Strings(names)
	for _, n := range names {
		g := r.gauges[n]
		g.mu.RLock()
		entries := make([]labeledValue, 0, len(g.vals))
		for k, p := range g.vals {
			entries = append(entries, labeledValue{k, p.Load()})
		}
		g.mu.RUnlock()
		sort.Slice(entries, func(i, j int) bool { return entries[i].key < entries[j].key })
		out = append(out, gaugeDump{
			name:    g.name,
			help:    g.help,
			keys:    g.keys,
			entries: entries,
		})
	}
	r.mu.RUnlock()
	return out
}

type counterDump struct {
	name    string
	help    string
	keys    []string
	entries []labeledValue
}

type gaugeDump struct {
	name    string
	help    string
	keys    []string
	entries []labeledValue
}

type labeledValue struct {
	key string
	v   float64
}

// joinLabels packs values into a delimiter-safe key. We use \x1F (Unit
// Separator) which is never legal in a Prometheus label value.
func joinLabels(values []string) string {
	if len(values) == 0 {
		return ""
	}
	if len(values) == 1 {
		return values[0]
	}
	out := values[0]
	for _, v := range values[1:] {
		out += "\x1F" + v
	}
	return out
}

func splitLabels(key string) []string {
	if key == "" {
		return nil
	}
	out := []string{}
	start := 0
	for i := 0; i < len(key); i++ {
		if key[i] == '\x1F' {
			out = append(out, key[start:i])
			start = i + 1
		}
	}
	out = append(out, key[start:])
	return out
}

// atomicFloat is a tiny wrapper around atomic.Uint64 storing the IEEE-754
// bit-pattern. Avoids pulling in sync/atomic's typed Float64 (Go 1.21+).
type atomicFloat struct{ v atomic.Uint64 }

func (a *atomicFloat) Store(f float64) { a.v.Store(math.Float64bits(f)) }
func (a *atomicFloat) Load() float64   { return math.Float64frombits(a.v.Load()) }
