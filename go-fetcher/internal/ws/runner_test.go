package ws

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	syncatomic "sync/atomic"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

// stubAdapter is a minimal Adapter for runner tests. It can be configured
// via the embedded fields to exercise different code paths.
type stubAdapter struct {
	name            string
	urlFunc         func() (string, error)
	subFrames       [][]byte
	parseFunc       func(frame []byte) (*Snapshot, error)
	heartbeat       []byte
	heartbeatEvery  time.Duration
	pongFor         func(frame []byte) []byte
	useLibPings     bool
	subscribeDelay  time.Duration
	maxSymbols      int
	decompressGzip  bool
	onReconnectCalled syncatomic.Int32
}

func (s *stubAdapter) Name() string                          { return s.name }
func (s *stubAdapter) URL(_ context.Context) (string, error) { return s.urlFunc() }
func (s *stubAdapter) BuildSubscribe(symbols []string) [][]byte {
	if s.subFrames != nil {
		return s.subFrames
	}
	// Default: send "subscribe:<symbols>" as one frame
	return [][]byte{[]byte("subscribe:" + strings.Join(symbols, ","))}
}
func (s *stubAdapter) Parse(frame []byte) (*Snapshot, error) {
	if s.parseFunc != nil {
		return s.parseFunc(frame)
	}
	return nil, nil
}
func (s *stubAdapter) Heartbeat() []byte                { return s.heartbeat }
func (s *stubAdapter) HeartbeatInterval() time.Duration { return s.heartbeatEvery }
func (s *stubAdapter) PongFor(frame []byte) []byte {
	if s.pongFor != nil {
		return s.pongFor(frame)
	}
	return nil
}
func (s *stubAdapter) UseLibPings() bool             { return s.useLibPings }
func (s *stubAdapter) SubscribeDelay() time.Duration { return s.subscribeDelay }
func (s *stubAdapter) MaxSymbols() int               { return s.maxSymbols }
func (s *stubAdapter) DecompressGzip() bool          { return s.decompressGzip }
func (s *stubAdapter) OnReconnect()                  { s.onReconnectCalled.Add(1) }

// newWSTestServer builds an httptest.Server that upgrades incoming
// requests to a WebSocket and runs the provided handler against the
// connection. Returns the ws:// URL.
func newWSTestServer(t *testing.T, handler func(*websocket.Conn)) (string, *httptest.Server) {
	t.Helper()
	up := websocket.Upgrader{
		CheckOrigin: func(r *http.Request) bool { return true },
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := up.Upgrade(w, r, nil)
		if err != nil {
			t.Logf("upgrade: %v", err)
			return
		}
		defer c.Close()
		handler(c)
	}))
	url := "ws" + strings.TrimPrefix(srv.URL, "http")
	return url, srv
}

func TestRunner_ParsesSnapshotsFromTestServer(t *testing.T) {
	var receivedSymbol string
	var receivedLevels int
	var hookMu sync.Mutex

	wsURL, srv := newWSTestServer(t, func(c *websocket.Conn) {
		// Read the subscribe frame from client
		_, _, err := c.ReadMessage()
		if err != nil {
			return
		}
		// Send one fake "data" frame
		_ = c.WriteMessage(websocket.TextMessage, []byte(`{"sym":"BTC","bids":[[60000,1.5]],"asks":[[60100,2.0]]}`))
		// Hold the conn open briefly so the runner can process before close
		time.Sleep(100 * time.Millisecond)
	})
	defer srv.Close()

	a := &stubAdapter{
		name:    "stub",
		urlFunc: func() (string, error) { return wsURL, nil },
		parseFunc: func(frame []byte) (*Snapshot, error) {
			// Pretend every frame contains BTC with 1 bid and 1 ask.
			return &Snapshot{
				Symbol: "BTC",
				Bids:   []Level{{60000, 1.5}},
				Asks:   []Level{{60100, 2.0}},
			}, nil
		},
		useLibPings: true,
	}
	r := NewRunner(a, func(ex string, snap Snapshot) {
		hookMu.Lock()
		receivedSymbol = snap.Symbol
		receivedLevels = len(snap.Bids) + len(snap.Asks)
		hookMu.Unlock()
	})
	r.SetSymbols([]string{"BTC"})

	ctx, cancel := context.WithTimeout(context.Background(), 1*time.Second)
	defer cancel()

	done := make(chan struct{})
	go func() {
		r.Run(ctx)
		close(done)
	}()
	<-done

	hookMu.Lock()
	defer hookMu.Unlock()
	if receivedSymbol != "BTC" {
		t.Errorf("symbol: want BTC got %q", receivedSymbol)
	}
	if receivedLevels != 2 {
		t.Errorf("levels: want 2 got %d", receivedLevels)
	}
}

func TestRunner_OnReconnectFiresOnFirstConnect(t *testing.T) {
	wsURL, srv := newWSTestServer(t, func(c *websocket.Conn) {
		_, _, _ = c.ReadMessage()
		time.Sleep(50 * time.Millisecond)
	})
	defer srv.Close()

	a := &stubAdapter{
		name:        "stub",
		urlFunc:     func() (string, error) { return wsURL, nil },
		useLibPings: true,
	}
	r := NewRunner(a, func(_ string, _ Snapshot) {})
	r.SetSymbols([]string{"BTC"})

	ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
	defer cancel()
	r.Run(ctx)

	if a.onReconnectCalled.Load() < 1 {
		t.Errorf("OnReconnect was not called (count=%d)", a.onReconnectCalled.Load())
	}
}

func TestRunner_SubscribeFrameTruncatedToMaxSymbols(t *testing.T) {
	// Server captures the subscribe frame to inspect what was sent.
	var subFrame string
	var subMu sync.Mutex
	wsURL, srv := newWSTestServer(t, func(c *websocket.Conn) {
		_, data, err := c.ReadMessage()
		if err != nil {
			return
		}
		subMu.Lock()
		subFrame = string(data)
		subMu.Unlock()
		time.Sleep(50 * time.Millisecond)
	})
	defer srv.Close()

	a := &stubAdapter{
		name:        "stub",
		urlFunc:     func() (string, error) { return wsURL, nil },
		maxSymbols:  3,
		useLibPings: true,
	}
	r := NewRunner(a, func(_ string, _ Snapshot) {})
	r.SetSymbols([]string{"A", "B", "C", "D", "E"}) // 5 symbols > max 3

	ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
	defer cancel()
	r.Run(ctx)

	subMu.Lock()
	got := subFrame
	subMu.Unlock()
	// stubAdapter default BuildSubscribe joins symbols comma-separated
	// after "subscribe:"; with max=3 we expect "subscribe:A,B,C"
	if !strings.HasPrefix(got, "subscribe:") {
		t.Fatalf("subscribe frame: %q", got)
	}
	syms := strings.TrimPrefix(got, "subscribe:")
	parts := strings.Split(syms, ",")
	if len(parts) != 3 {
		t.Errorf("MaxSymbols cap should truncate to 3, got %d: %v", len(parts), parts)
	}
}

func TestRunner_FailedDialBackoffAndRetry(t *testing.T) {
	// First two dial attempts fail (server doesn't exist), then succeed.
	failCount := syncatomic.Int32{}
	var srv *httptest.Server
	wsURL := "ws://127.0.0.1:1" // closed port → connection refused initially
	_ = wsURL

	// Use a real server and have URL func return a bogus address until we flip
	realURL, s := newWSTestServer(t, func(c *websocket.Conn) {
		_, _, _ = c.ReadMessage()
		time.Sleep(50 * time.Millisecond)
	})
	srv = s
	defer srv.Close()

	a := &stubAdapter{
		name: "stub",
		urlFunc: func() (string, error) {
			n := failCount.Add(1)
			if n < 2 {
				return "ws://127.0.0.1:1/nope", nil // refused
			}
			return realURL, nil
		},
		useLibPings: true,
	}
	r := NewRunner(a, func(_ string, _ Snapshot) {})
	r.SetSymbols([]string{"BTC"})

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	r.Run(ctx)

	// Expect: 1 failed dial + 1 successful dial = OnReconnect called ≥2
	if a.onReconnectCalled.Load() < 2 {
		t.Errorf("expected ≥2 reconnect attempts (failed + retry), got %d",
			a.onReconnectCalled.Load())
	}
}

func TestRunner_ParseErrorContinuesProcessing(t *testing.T) {
	// Server sends two frames: first triggers a parse error, second succeeds.
	// Runner should NOT exit on the error — it should keep processing.
	dataReceived := syncatomic.Int32{}

	wsURL, srv := newWSTestServer(t, func(c *websocket.Conn) {
		_, _, _ = c.ReadMessage() // subscribe
		_ = c.WriteMessage(websocket.TextMessage, []byte("BAD"))
		_ = c.WriteMessage(websocket.TextMessage, []byte("GOOD"))
		time.Sleep(150 * time.Millisecond)
	})
	defer srv.Close()

	a := &stubAdapter{
		name:    "stub",
		urlFunc: func() (string, error) { return wsURL, nil },
		parseFunc: func(frame []byte) (*Snapshot, error) {
			if string(frame) == "BAD" {
				return nil, errStubParse
			}
			dataReceived.Add(1)
			return &Snapshot{Symbol: "BTC", Bids: []Level{{1, 1}}}, nil
		},
		useLibPings: true,
	}
	r := NewRunner(a, func(_ string, _ Snapshot) {})
	r.SetSymbols([]string{"BTC"})

	ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
	defer cancel()
	r.Run(ctx)

	if dataReceived.Load() < 1 {
		t.Errorf("good frame after parse error wasn't processed")
	}
}

var errStubParse = &stubParseError{}

type stubParseError struct{}

func (e *stubParseError) Error() string { return "stub parse error" }

func TestRunner_PongForRespondsToInboundPing(t *testing.T) {
	// Server sends "ping", expects the runner to forward to PongFor and
	// then write back the returned bytes.
	pingFromClient := make(chan string, 1)

	wsURL, srv := newWSTestServer(t, func(c *websocket.Conn) {
		_, _, _ = c.ReadMessage() // subscribe
		_ = c.WriteMessage(websocket.TextMessage, []byte("ping"))
		// Read what the client sends back
		_, reply, err := c.ReadMessage()
		if err == nil {
			pingFromClient <- string(reply)
		}
		time.Sleep(50 * time.Millisecond)
	})
	defer srv.Close()

	a := &stubAdapter{
		name:    "stub",
		urlFunc: func() (string, error) { return wsURL, nil },
		pongFor: func(frame []byte) []byte {
			if string(frame) == "ping" {
				return []byte("pong")
			}
			return nil
		},
		useLibPings: true,
	}
	r := NewRunner(a, func(_ string, _ Snapshot) {})
	r.SetSymbols([]string{"BTC"})

	ctx, cancel := context.WithTimeout(context.Background(), 1*time.Second)
	defer cancel()
	go r.Run(ctx)

	select {
	case got := <-pingFromClient:
		if got != "pong" {
			t.Errorf("PongFor reply: want pong got %q", got)
		}
	case <-time.After(500 * time.Millisecond):
		t.Errorf("PongFor reply not sent (timeout)")
	}
	cancel()
}
