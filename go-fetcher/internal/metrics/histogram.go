package metrics

import (
	"math"
	"sort"
	"sync"
	"sync/atomic"
)

// Histogram observes float64 samples (typically durations in seconds)
// into pre-configured cumulative buckets, matching Prometheus's
// histogram semantics: each bucket counts samples ≤ its upper bound,
// plus a +Inf bucket of total count and a running sum for the mean.
//
// Cardinality: as with Counter, label keys are pinned at registration
// time; the caller picks the values. A 7-bucket histogram × 18 venues
// is 126 series — fine. Don't add per-symbol labels.
type Histogram struct {
	name    string
	help    string
	keys    []string
	buckets []float64 // strictly-increasing upper bounds

	mu    sync.RWMutex
	vals  map[string]*histSeries // label-value-tuple → series
}

type histSeries struct {
	counts []atomic.Uint64 // len == len(buckets) + 1 (last is +Inf bucket = total)
	sum    atomic.Uint64   // bit-pattern of float64 cumulative sum
}

// NewHistogram registers (or returns the existing) Histogram on Default.
// `buckets` must be sorted strictly increasing; a +Inf bucket is added
// implicitly by the renderer.
func NewHistogram(name, help string, buckets []float64, labelKeys ...string) *Histogram {
	return Default.NewHistogram(name, help, buckets, labelKeys...)
}

func (r *Registry) NewHistogram(name, help string, buckets []float64, labelKeys ...string) *Histogram {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.histograms == nil {
		r.histograms = make(map[string]*Histogram)
	}
	if h, ok := r.histograms[name]; ok {
		return h
	}
	sorted := append([]float64(nil), buckets...)
	sort.Float64s(sorted)
	h := &Histogram{
		name:    name,
		help:    help,
		keys:    append([]string(nil), labelKeys...),
		buckets: sorted,
		vals:    make(map[string]*histSeries),
	}
	r.histograms[name] = h
	return h
}

// Observe records a single sample for the matching label values.
// Cost: one RLock + atomic ops; cumulative bump of N buckets where N
// is the number of buckets ≥ the sample's index.
func (h *Histogram) Observe(v float64, labelValues ...string) {
	if len(labelValues) != len(h.keys) {
		return
	}
	key := joinLabels(labelValues)
	h.mu.RLock()
	s, ok := h.vals[key]
	h.mu.RUnlock()
	if !ok {
		h.mu.Lock()
		s, ok = h.vals[key]
		if !ok {
			s = &histSeries{counts: make([]atomic.Uint64, len(h.buckets)+1)}
			h.vals[key] = s
		}
		h.mu.Unlock()
	}
	idx := sort.SearchFloat64s(h.buckets, v)
	// Cumulative: bump bucket idx..len(buckets) inclusive. If idx ==
	// len(buckets), only the +Inf bucket counts.
	for i := idx; i <= len(h.buckets); i++ {
		s.counts[i].Add(1)
	}
	// Sum: CAS loop on the bit-pattern.
	for {
		old := s.sum.Load()
		nw := math.Float64bits(math.Float64frombits(old) + v)
		if s.sum.CompareAndSwap(old, nw) {
			break
		}
	}
}

// histogramDump is the snapshot shape the exporter consumes.
type histogramDump struct {
	name    string
	help    string
	keys    []string
	buckets []float64
	entries []histEntry
}

type histEntry struct {
	labels []string
	counts []uint64 // len == len(buckets) + 1 (+Inf last)
	sum    float64
}

func (r *Registry) snapshotHistograms() []histogramDump {
	r.mu.RLock()
	out := make([]histogramDump, 0, len(r.histograms))
	names := make([]string, 0, len(r.histograms))
	for n := range r.histograms {
		names = append(names, n)
	}
	sort.Strings(names)
	for _, n := range names {
		h := r.histograms[n]
		h.mu.RLock()
		entries := make([]histEntry, 0, len(h.vals))
		for k, s := range h.vals {
			cnt := make([]uint64, len(s.counts))
			for i := range s.counts {
				cnt[i] = s.counts[i].Load()
			}
			entries = append(entries, histEntry{
				labels: splitLabels(k),
				counts: cnt,
				sum:    math.Float64frombits(s.sum.Load()),
			})
		}
		h.mu.RUnlock()
		sort.Slice(entries, func(i, j int) bool {
			return joinLabels(entries[i].labels) < joinLabels(entries[j].labels)
		})
		out = append(out, histogramDump{
			name:    h.name,
			help:    h.help,
			keys:    h.keys,
			buckets: h.buckets,
			entries: entries,
		})
	}
	r.mu.RUnlock()
	return out
}
