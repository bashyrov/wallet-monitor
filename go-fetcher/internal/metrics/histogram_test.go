package metrics

import (
	"strings"
	"sync"
	"testing"
)

func TestHistogram_BucketAssignment(t *testing.T) {
	r := NewRegistry()
	h := r.NewHistogram("h", "help", []float64{0.1, 0.2, 0.3}, "venue")
	h.Observe(0.05, "binance")  // → bucket 0.1 (and 0.2, 0.3, +Inf)
	h.Observe(0.15, "binance")  // → bucket 0.2 (and 0.3, +Inf)
	h.Observe(0.99, "binance")  // → +Inf only

	out := string(r.RenderProm())

	// Cumulative semantics:
	//   le="0.1"   count = 1 (the 0.05)
	//   le="0.2"   count = 2 (0.05 + 0.15)
	//   le="0.3"   count = 2
	//   le="+Inf"  count = 3 (all)
	if !strings.Contains(out, `h_bucket{venue="binance",le="0.1"} 1`) {
		t.Errorf("le=0.1 bucket count wrong: %s", out)
	}
	if !strings.Contains(out, `h_bucket{venue="binance",le="0.2"} 2`) {
		t.Errorf("le=0.2 bucket count wrong: %s", out)
	}
	if !strings.Contains(out, `h_bucket{venue="binance",le="0.3"} 2`) {
		t.Errorf("le=0.3 bucket count wrong: %s", out)
	}
	if !strings.Contains(out, `h_bucket{venue="binance",le="+Inf"} 3`) {
		t.Errorf("+Inf bucket count wrong: %s", out)
	}
	if !strings.Contains(out, `h_count{venue="binance"} 3`) {
		t.Errorf("_count missing or wrong: %s", out)
	}
	// _sum = 0.05 + 0.15 + 0.99 = 1.19
	if !strings.Contains(out, `h_sum{venue="binance"} 1.19`) {
		t.Errorf("_sum wrong: %s", out)
	}
}

func TestHistogram_MisArityDrops(t *testing.T) {
	r := NewRegistry()
	h := r.NewHistogram("hm", "help", []float64{0.1, 0.2}, "venue", "kind")
	h.Observe(0.05, "only_one") // mis-arity
	h.Observe(0.05, "binance", "ws")
	out := string(r.RenderProm())
	if strings.Contains(out, "only_one") {
		t.Errorf("mis-arity must drop, got: %s", out)
	}
	if !strings.Contains(out, `hm_count{venue="binance",kind="ws"} 1`) {
		t.Errorf("legit observe lost: %s", out)
	}
}

func TestHistogram_TypeLineRendered(t *testing.T) {
	r := NewRegistry()
	r.NewHistogram("named", "the-help", []float64{0.1}, "v").Observe(0.05, "x")
	out := string(r.RenderProm())
	if !strings.Contains(out, "# TYPE named histogram") {
		t.Errorf("TYPE line missing: %s", out)
	}
	if !strings.Contains(out, "# HELP named the-help") {
		t.Errorf("HELP line missing: %s", out)
	}
}

func TestHistogram_Concurrent(t *testing.T) {
	r := NewRegistry()
	h := r.NewHistogram("hc", "help", []float64{0.1, 1.0}, "venue")
	var wg sync.WaitGroup
	for i := 0; i < 50; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 200; j++ {
				h.Observe(0.5, "binance") // → between 0.1 and 1.0, so le=1.0 +Inf
			}
		}()
	}
	wg.Wait()
	// 50 * 200 = 10000 samples each. Expect:
	//   le="0.1"  count = 0
	//   le="1.0"  count = 10000
	//   +Inf      count = 10000
	out := string(r.RenderProm())
	if !strings.Contains(out, `hc_bucket{venue="binance",le="0.1"} 0`) {
		t.Errorf("0.1 should be 0: %s", out)
	}
	if !strings.Contains(out, `hc_bucket{venue="binance",le="1"} 10000`) {
		t.Errorf("1.0 cumulative count wrong: %s", out)
	}
	if !strings.Contains(out, `hc_bucket{venue="binance",le="+Inf"} 10000`) {
		t.Errorf("+Inf count wrong: %s", out)
	}
	if !strings.Contains(out, `hc_count{venue="binance"} 10000`) {
		t.Errorf("_count wrong: %s", out)
	}
}

func TestHistogram_SampleExceedsAllBuckets(t *testing.T) {
	r := NewRegistry()
	h := r.NewHistogram("he", "help", []float64{0.1, 0.2}, "venue")
	h.Observe(99.0, "binance")
	out := string(r.RenderProm())
	if !strings.Contains(out, `he_bucket{venue="binance",le="0.1"} 0`) {
		t.Errorf("0.1 should be 0 (sample exceeded all finite buckets)")
	}
	if !strings.Contains(out, `he_bucket{venue="binance",le="0.2"} 0`) {
		t.Errorf("0.2 should be 0")
	}
	if !strings.Contains(out, `he_bucket{venue="binance",le="+Inf"} 1`) {
		t.Errorf("+Inf must catch it: %s", out)
	}
}

func TestRegistry_HistogramReregisterReturnsSame(t *testing.T) {
	r := NewRegistry()
	h1 := r.NewHistogram("dup", "h1", []float64{0.1}, "v")
	h2 := r.NewHistogram("dup", "h2-ignored", []float64{0.5}, "x")
	if h1 != h2 {
		t.Errorf("re-register must return existing histogram")
	}
}
