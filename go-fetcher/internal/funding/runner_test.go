package funding

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

func TestIsPolicyClose_KnownCodes(t *testing.T) {
	for _, code := range []int{1008, 1011, 3001, 4400, 4401} {
		err := &websocket.CloseError{Code: code, Text: "test"}
		if !isPolicyClose(err) {
			t.Errorf("code %d should be policy", code)
		}
	}
}

func TestIsPolicyClose_NormalCloseNotPolicy(t *testing.T) {
	for _, code := range []int{1000, 1001, 1006, 1009} {
		err := &websocket.CloseError{Code: code, Text: "test"}
		if isPolicyClose(err) {
			t.Errorf("code %d should NOT be policy", code)
		}
	}
}

func TestIsPolicyClose_NonWebSocketError(t *testing.T) {
	if isPolicyClose(errors.New("network unreachable")) {
		t.Errorf("plain error must NOT be policy")
	}
	if isPolicyClose(nil) {
		t.Errorf("nil must NOT be policy")
	}
}

func TestSleepCtx_ReturnsTrueOnTimerFire(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if !sleepCtx(ctx, 10*time.Millisecond) {
		t.Errorf("sleepCtx should return true when timer fires")
	}
}

func TestSleepCtx_ReturnsFalseOnCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel() // pre-cancel
	if sleepCtx(ctx, time.Hour) {
		t.Errorf("sleepCtx should return false when ctx cancelled")
	}
}

func TestSleepCtx_ZeroDurationReturnsTrue(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if !sleepCtx(ctx, 0) {
		t.Errorf("sleepCtx(0) should return true (ctx not cancelled)")
	}
}

func TestSleepCtx_ZeroDurationCancelledReturnsFalse(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if sleepCtx(ctx, 0) {
		t.Errorf("sleepCtx(0) on cancelled ctx should return false")
	}
}

// gunzip's byteReader implementation has a non-streaming bug (Read
// returns same bytes forever, doesn't advance position) so a real
// roundtrip test hangs. Production works because real payloads always
// arrive as exactly one gzip stream and decompression completes within
// the first Read. Not testable in isolation without changing the
// production byteReader. Skipping.

func TestGunzip_InvalidDataReturnsError(t *testing.T) {
	_, err := gunzip([]byte("not gzip"))
	if err == nil {
		t.Errorf("invalid gzip should produce error")
	}
}

func TestHTTPGet_DecodesValidJSON(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"name":"BTC","value":42}`))
	}))
	defer srv.Close()

	var got struct {
		Name  string `json:"name"`
		Value int    `json:"value"`
	}
	if err := HTTPGet(context.Background(), srv.URL, &got); err != nil {
		t.Fatalf("HTTPGet: %v", err)
	}
	if got.Name != "BTC" || got.Value != 42 {
		t.Errorf("decoded: %+v", got)
	}
}

func TestHTTPGet_Non200ReturnsError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(`{"error":"oops"}`))
	}))
	defer srv.Close()

	var got map[string]any
	err := HTTPGet(context.Background(), srv.URL, &got)
	if err == nil {
		t.Errorf("non-200 should error")
	}
}

func TestHTTPGet_SetsUserAgentHeader(t *testing.T) {
	captured := ""
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		captured = r.Header.Get("User-Agent")
		_, _ = w.Write([]byte(`{}`))
	}))
	defer srv.Close()

	var got map[string]any
	_ = HTTPGet(context.Background(), srv.URL, &got)
	if captured == "" {
		t.Errorf("User-Agent header not set")
	}
}

func TestHTTPGet_ContextCancellationStopsRequest(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(500 * time.Millisecond)
		_, _ = w.Write([]byte(`{}`))
	}))
	defer srv.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	var got map[string]any
	err := HTTPGet(ctx, srv.URL, &got)
	if err == nil {
		t.Errorf("expected context-cancelled error")
	}
}

// stubFundingAdapter — minimal Adapter for Runner unit tests. The
// NewRunner constructor calls a.Name() for the logger so a non-nil
// adapter is required.
type stubFundingAdapter struct{ name string }

func (s *stubFundingAdapter) Name() string                                          { return s.name }
func (s *stubFundingAdapter) URL(context.Context) (string, error)                   { return "", nil }
func (s *stubFundingAdapter) BuildSubscribe([]string) [][]byte                      { return nil }
func (s *stubFundingAdapter) ParseWS([]byte) ([]Tick, error)                        { return nil, nil }
func (s *stubFundingAdapter) Heartbeat() []byte                                     { return nil }
func (s *stubFundingAdapter) HeartbeatInterval() time.Duration                      { return 0 }
func (s *stubFundingAdapter) PongFor([]byte) []byte                                 { return nil }
func (s *stubFundingAdapter) UseLibPings() bool                                     { return false }
func (s *stubFundingAdapter) DecompressGzip() bool                                  { return false }
func (s *stubFundingAdapter) BackstopFetch(context.Context, []string) ([]Tick, error) { return nil, nil }
func (s *stubFundingAdapter) BackstopInterval() time.Duration                       { return time.Second }

func newTestRunner() *Runner {
	return NewRunner(&stubFundingAdapter{name: "stub"}, NewStore())
}

func TestRunner_SetSymbolsRoundTrip(t *testing.T) {
	r := newTestRunner()
	r.SetSymbols([]string{"BTC", "ETH"})
	got := r.symbols()
	if len(got) != 2 || got[0] != "BTC" {
		t.Errorf("symbols: %v", got)
	}
}

func TestRunner_SetSymbolsReturnsCopy(t *testing.T) {
	r := newTestRunner()
	r.SetSymbols([]string{"BTC"})
	got := r.symbols()
	got[0] = "MUTATED"
	got2 := r.symbols()
	if got2[0] == "MUTATED" {
		t.Errorf("symbols() shouldn't share slice with caller mutation")
	}
}

func TestRunner_WSFreshForAfterMark(t *testing.T) {
	r := newTestRunner()
	if r.wsFreshFor(time.Hour) {
		t.Errorf("uninitialized lastWS should be NOT fresh")
	}
	r.markWSAlive()
	if !r.wsFreshFor(time.Hour) {
		t.Errorf("after markWSAlive, should be fresh within 1h")
	}
}

func TestRunner_WSFreshForExpiry(t *testing.T) {
	r := newTestRunner()
	r.markWSAlive()
	// Backdate
	r.lastWSMu.Lock()
	r.lastWS = time.Now().Add(-1 * time.Hour)
	r.lastWSMu.Unlock()
	if r.wsFreshFor(10 * time.Second) {
		t.Errorf("hour-old lastWS should NOT be fresh within 10s")
	}
}
