package metrics

import (
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

func TestCounter_AddInc(t *testing.T) {
	r := NewRegistry()
	c := r.NewCounter("test_counter", "help", "venue")
	c.Inc("binance")
	c.Inc("binance")
	c.Add(5, "binance")
	c.Inc("bybit")
	out := string(r.RenderProm())
	if !strings.Contains(out, `test_counter{venue="binance"} 7`) {
		t.Errorf("binance counter wrong: %s", out)
	}
	if !strings.Contains(out, `test_counter{venue="bybit"} 1`) {
		t.Errorf("bybit counter wrong: %s", out)
	}
}

func TestCounter_MisArityDrops(t *testing.T) {
	r := NewRegistry()
	c := r.NewCounter("c", "h", "venue", "source")
	c.Inc("only_one") // mis-arity (need 2)
	c.Inc("binance", "ws")
	out := string(r.RenderProm())
	if strings.Contains(out, "only_one") {
		t.Errorf("mis-arity should drop silently, got: %s", out)
	}
	if !strings.Contains(out, `c{venue="binance",source="ws"} 1`) {
		t.Errorf("legit call lost: %s", out)
	}
}

func TestGauge_SetOverwrites(t *testing.T) {
	r := NewRegistry()
	g := r.NewGauge("g", "h", "venue")
	g.Set(1.5, "binance")
	g.Set(2.5, "binance") // overwrites
	g.Set(3.0, "bybit")
	out := string(r.RenderProm())
	if !strings.Contains(out, `g{venue="binance"} 2.5`) {
		t.Errorf("binance gauge wrong: %s", out)
	}
	if !strings.Contains(out, `g{venue="bybit"} 3`) {
		t.Errorf("bybit gauge wrong: %s", out)
	}
}

func TestHTTPHandler_ServesProm(t *testing.T) {
	NewCounter("served_test", "served-test help", "venue").Inc("binance")
	srv := httptest.NewServer(HTTPHandler())
	defer srv.Close()

	resp, err := srv.Client().Get(srv.URL + "/metrics")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	defer resp.Body.Close()
	if resp.Header.Get("Content-Type") != "text/plain; version=0.0.4; charset=utf-8" {
		t.Errorf("wrong content-type: %s", resp.Header.Get("Content-Type"))
	}
	buf := make([]byte, 4096)
	n, _ := resp.Body.Read(buf)
	body := string(buf[:n])
	if !strings.Contains(body, "# HELP served_test served-test help") {
		t.Errorf("HELP line missing: %s", body)
	}
	if !strings.Contains(body, "# TYPE served_test counter") {
		t.Errorf("TYPE line missing: %s", body)
	}
	if !strings.Contains(body, `served_test{venue="binance"} 1`) {
		t.Errorf("sample missing: %s", body)
	}
}

func TestCounter_Concurrent(t *testing.T) {
	r := NewRegistry()
	c := r.NewCounter("concurrent", "h", "venue")
	var wg sync.WaitGroup
	for i := 0; i < 100; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 100; j++ {
				c.Inc("binance")
			}
		}()
	}
	wg.Wait()
	out := string(r.RenderProm())
	if !strings.Contains(out, `concurrent{venue="binance"} 10000`) {
		t.Errorf("concurrent counter race / loss: %s", out)
	}
}

func TestEscapeLabelValue(t *testing.T) {
	cases := map[string]string{
		"plain":     "plain",
		`with"q`:    `with\"q`,
		`back\slash`: `back\\slash`,
		"new\nline": `new\nline`,
	}
	for in, want := range cases {
		if got := escapeLabelValue(in); got != want {
			t.Errorf("escape %q: got %q want %q", in, got, want)
		}
	}
}

func TestRegistry_RegisterIsIdempotent(t *testing.T) {
	r := NewRegistry()
	c1 := r.NewCounter("dup", "h", "venue")
	c2 := r.NewCounter("dup", "different help, ignored", "different_key")
	if c1 != c2 {
		t.Errorf("re-register must return existing instance")
	}
}

func TestPipeline_RecordBookStore(t *testing.T) {
	booksStored = NewCounter("avalant_book_store_total_test", "test", "venue", "source")
	Pipeline{}.RecordBookStore("gate", "ws")
	out := string(Default.RenderProm())
	if !strings.Contains(out, `avalant_book_store_total_test{venue="gate",source="ws"} 1`) {
		t.Errorf("pipeline counter not recorded: %s", out)
	}
}

func TestSplitLabels(t *testing.T) {
	got := splitLabels("a\x1Fb\x1Fc")
	if len(got) != 3 || got[0] != "a" || got[1] != "b" || got[2] != "c" {
		t.Errorf("splitLabels wrong: %v", got)
	}
	if x := splitLabels(""); len(x) != 0 {
		t.Errorf("empty must return nil, got %v", x)
	}
	if x := splitLabels("alone"); len(x) != 1 || x[0] != "alone" {
		t.Errorf("single value wrong: %v", x)
	}
}
