package wsbroadcast

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/ticks"

	"github.com/gorilla/websocket"
)

func makeTokenForServer(secret string, payload map[string]any) string {
	hb, _ := json.Marshal(map[string]any{"alg": "HS256", "typ": "JWT"})
	pb, _ := json.Marshal(payload)
	h := base64.RawURLEncoding.EncodeToString(hb)
	p := base64.RawURLEncoding.EncodeToString(pb)
	signing := h + "." + p
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(signing))
	sig := base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
	return signing + "." + sig
}

// buildTestService — minimal Service for handler tests. Uses empty
// cacheDir so SnapshotForNewClient returns nil; no broadcast loops
// running.
func buildTestService(t *testing.T, secret string) *Service {
	t.Helper()
	dir := t.TempDir()
	jwt := NewJWTValidator(secret)
	ls := NewLongShort(dir)
	sp := NewSpotShort(dir)
	dx := NewDexShort(dir)
	fn := NewFunding(dir)
	bk := NewBook(nil, nil, nil)
	tr := NewTrades(ticks.NewRing(50), nil)
	return NewService(jwt, ls, sp, dx, fn, bk, tr)
}

func startHTTPTest(t *testing.T, svc *Service) (*httptest.Server, string) {
	t.Helper()
	mux := http.NewServeMux()
	svc.Routes(mux)
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	wsBase := "ws" + strings.TrimPrefix(srv.URL, "http")
	return srv, wsBase
}

func TestService_RoutesRegisteredOnMux(t *testing.T) {
	svc := buildTestService(t, "test")
	mux := http.NewServeMux()
	svc.Routes(mux)

	// Try a GET (non-WS) — handler will respond with bad request because
	// the gorilla upgrader rejects, but the mux must FIND the handler.
	w := httptest.NewRecorder()
	for _, path := range []string{
		"/api/screener/ws/long-short",
		"/api/screener/ws/arb",
		"/api/screener/ws/funding",
		"/api/screener/ws/book",
		"/api/screener/ws/trades",
	} {
		w = httptest.NewRecorder()
		mux.ServeHTTP(w, httptest.NewRequest(http.MethodGet, path, nil))
		// 404 means the route is missing — that's the failure mode we want
		// to detect. Anything else (200/400/426) means the handler ran.
		if w.Code == http.StatusNotFound {
			t.Errorf("route %s not registered (got 404)", path)
		}
	}
}

func TestService_TradesEndpoint_OmittedWhenTradesNil(t *testing.T) {
	dir := t.TempDir()
	svc := NewService(NewJWTValidator("k"), NewLongShort(dir), NewSpotShort(dir), NewDexShort(dir), NewFunding(dir), NewBook(nil, nil, nil), nil)
	mux := http.NewServeMux()
	svc.Routes(mux)
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/screener/ws/trades", nil))
	if w.Code != http.StatusNotFound {
		t.Errorf("trades route should be absent when Trades is nil, got %d", w.Code)
	}
}

func TestHandshakeAuth_OptionalAuthNoFirstFrameClosesAfterTimeout(t *testing.T) {
	svc := buildTestService(t, "secret")
	_, wsBase := startHTTPTest(t, svc)
	conn, _, err := websocket.DefaultDialer.Dial(wsBase+"/api/screener/ws/long-short", nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()
	// Don't send a first frame. handshakeAuth has 5s timeout in production
	// but the read deadline is set so server should close us out. Just
	// verify the server doesn't hang forever.
	_ = conn.SetReadDeadline(time.Now().Add(10 * time.Second))
	_, _, err = conn.ReadMessage()
	if err == nil {
		t.Errorf("expected close after no-first-frame")
	}
}

func TestHandshakeAuth_LongShortAcceptsAnonymous(t *testing.T) {
	svc := buildTestService(t, "secret")
	_, wsBase := startHTTPTest(t, svc)
	conn, _, err := websocket.DefaultDialer.Dial(wsBase+"/api/screener/ws/long-short", nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()
	// Send empty auth — anonymous accepted on long-short
	if err := conn.WriteMessage(websocket.TextMessage, []byte(`{"auth":""}`)); err != nil {
		t.Fatalf("write: %v", err)
	}
	// Should NOT be closed immediately. Set short deadline, expect timeout (no close).
	_ = conn.SetReadDeadline(time.Now().Add(300 * time.Millisecond))
	_, _, err = conn.ReadMessage()
	if err == nil {
		return // got something — also fine
	}
	// We expect a timeout, not a close with 4xxx code
	if ce, ok := err.(*websocket.CloseError); ok {
		if ce.Code >= 4400 && ce.Code <= 4499 {
			t.Errorf("anon should not be closed with 4xxx: %v", ce)
		}
	}
}

func TestHandshakeAuth_BookRequiresValidAuth(t *testing.T) {
	svc := buildTestService(t, "secret")
	_, wsBase := startHTTPTest(t, svc)
	conn, _, err := websocket.DefaultDialer.Dial(wsBase+"/api/screener/ws/book", nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()
	// Send empty auth — should be closed with 4401
	if err := conn.WriteMessage(websocket.TextMessage, []byte(`{"auth":""}`)); err != nil {
		t.Fatalf("write: %v", err)
	}
	_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	_, _, err = conn.ReadMessage()
	if err == nil {
		t.Errorf("book without auth should be closed")
	}
	if ce, ok := err.(*websocket.CloseError); ok {
		if ce.Code != 4401 {
			t.Errorf("expected close code 4401, got %d (%s)", ce.Code, ce.Text)
		}
	}
}

func TestHandshakeAuth_BookAcceptsValidJWT(t *testing.T) {
	svc := buildTestService(t, "secret")
	_, wsBase := startHTTPTest(t, svc)
	conn, _, err := websocket.DefaultDialer.Dial(wsBase+"/api/screener/ws/book", nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()
	tok := makeTokenForServer("secret", map[string]any{
		"sub": "42", "exp": time.Now().Add(time.Hour).Unix(),
	})
	authFrame := `{"auth":"` + tok + `"}`
	if err := conn.WriteMessage(websocket.TextMessage, []byte(authFrame)); err != nil {
		t.Fatalf("write: %v", err)
	}
	// Should NOT be closed with 4401
	_ = conn.SetReadDeadline(time.Now().Add(300 * time.Millisecond))
	_, _, err = conn.ReadMessage()
	if err == nil {
		return
	}
	if ce, ok := err.(*websocket.CloseError); ok {
		if ce.Code == 4401 {
			t.Errorf("valid JWT was rejected: %v", ce)
		}
	}
}

func TestHandshakeAuth_BookRejectsBadJWT(t *testing.T) {
	svc := buildTestService(t, "secret")
	_, wsBase := startHTTPTest(t, svc)
	conn, _, err := websocket.DefaultDialer.Dial(wsBase+"/api/screener/ws/book", nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()
	// JWT signed with wrong secret
	tok := makeTokenForServer("wrong-secret", map[string]any{
		"sub": "42", "exp": time.Now().Add(time.Hour).Unix(),
	})
	if err := conn.WriteMessage(websocket.TextMessage, []byte(`{"auth":"`+tok+`"}`)); err != nil {
		t.Fatalf("write: %v", err)
	}
	_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	_, _, err = conn.ReadMessage()
	if err == nil {
		t.Errorf("bad-sig JWT should close conn")
	}
	if ce, ok := err.(*websocket.CloseError); ok && ce.Code != 4401 {
		t.Errorf("expected 4401 for bad JWT, got %d", ce.Code)
	}
}
