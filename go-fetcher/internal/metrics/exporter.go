package metrics

import (
	"bytes"
	"net/http"
	"strconv"
	"strings"
)

// HTTPHandler returns an http.Handler that renders the Default registry
// in Prometheus text-exposition format. Mount at /metrics.
func HTTPHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
		w.Write(Default.RenderProm())
	})
}

// RenderProm produces a Prometheus text-exposition byte slice.
// Format reference: https://prometheus.io/docs/instrumenting/exposition_formats/
func (r *Registry) RenderProm() []byte {
	var b bytes.Buffer

	for _, c := range r.snapshotCounters() {
		if c.help != "" {
			b.WriteString("# HELP " + c.name + " " + c.help + "\n")
		}
		b.WriteString("# TYPE " + c.name + " counter\n")
		for _, e := range c.entries {
			writeSample(&b, c.name, c.keys, splitLabels(e.key), e.v)
		}
	}
	for _, g := range r.snapshotGauges() {
		if g.help != "" {
			b.WriteString("# HELP " + g.name + " " + g.help + "\n")
		}
		b.WriteString("# TYPE " + g.name + " gauge\n")
		for _, e := range g.entries {
			writeSample(&b, g.name, g.keys, splitLabels(e.key), e.v)
		}
	}
	for _, h := range r.snapshotHistograms() {
		if h.help != "" {
			b.WriteString("# HELP " + h.name + " " + h.help + "\n")
		}
		b.WriteString("# TYPE " + h.name + " histogram\n")
		for _, e := range h.entries {
			// One sample per bucket boundary, then _sum and _count.
			for i, ub := range h.buckets {
				ubStr := strconv.FormatFloat(ub, 'g', -1, 64)
				labels := append(append([]string{}, e.labels...), ubStr)
				keys := append(append([]string{}, h.keys...), "le")
				writeSample(&b, h.name+"_bucket", keys, labels, float64(e.counts[i]))
			}
			labelsInf := append(append([]string{}, e.labels...), "+Inf")
			keysInf := append(append([]string{}, h.keys...), "le")
			writeSample(&b, h.name+"_bucket", keysInf, labelsInf, float64(e.counts[len(h.buckets)]))
			writeSample(&b, h.name+"_sum", h.keys, e.labels, e.sum)
			writeSample(&b, h.name+"_count", h.keys, e.labels, float64(e.counts[len(h.buckets)]))
		}
	}
	return b.Bytes()
}

func writeSample(b *bytes.Buffer, name string, keys, values []string, v float64) {
	b.WriteString(name)
	if len(keys) > 0 {
		b.WriteByte('{')
		for i := 0; i < len(keys) && i < len(values); i++ {
			if i > 0 {
				b.WriteByte(',')
			}
			b.WriteString(keys[i])
			b.WriteString(`="`)
			b.WriteString(escapeLabelValue(values[i]))
			b.WriteByte('"')
		}
		b.WriteByte('}')
	}
	b.WriteByte(' ')
	b.WriteString(strconv.FormatFloat(v, 'g', -1, 64))
	b.WriteByte('\n')
}

// escapeLabelValue per Prom text format: backslashes, quotes, newlines.
func escapeLabelValue(s string) string {
	if !strings.ContainsAny(s, `\"`+"\n") {
		return s
	}
	var b strings.Builder
	for _, r := range s {
		switch r {
		case '\\':
			b.WriteString(`\\`)
		case '"':
			b.WriteString(`\"`)
		case '\n':
			b.WriteString(`\n`)
		default:
			b.WriteRune(r)
		}
	}
	return b.String()
}
