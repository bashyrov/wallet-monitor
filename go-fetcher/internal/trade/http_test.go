package trade

import (
	"net/http"
	"testing"
	"time"
)

func TestNewHTTPClient_TimeoutSet(t *testing.T) {
	c := NewHTTPClient(15 * time.Second)
	if c.Timeout != 15*time.Second {
		t.Errorf("timeout: want 15s got %v", c.Timeout)
	}
}

func TestNewHTTPClient_TransportTunedForVenues(t *testing.T) {
	c := NewHTTPClient(10 * time.Second)
	tr, ok := c.Transport.(*http.Transport)
	if !ok {
		t.Fatalf("Transport: want *http.Transport, got %T", c.Transport)
	}
	// HTTP/2 negotiation must be on (binance/bybit/okx support it).
	if !tr.ForceAttemptHTTP2 {
		t.Errorf("ForceAttemptHTTP2 must be true")
	}
	// 5min idle keeps TLS warm across infrequent bursts (per code comment).
	if tr.IdleConnTimeout != 300*time.Second {
		t.Errorf("IdleConnTimeout: want 300s got %v", tr.IdleConnTimeout)
	}
	// Per-host pool sized for parallel arb legs.
	if tr.MaxIdleConnsPerHost != 32 {
		t.Errorf("MaxIdleConnsPerHost: want 32 got %d", tr.MaxIdleConnsPerHost)
	}
	if tr.MaxConnsPerHost != 64 {
		t.Errorf("MaxConnsPerHost: want 64 got %d", tr.MaxConnsPerHost)
	}
	// Fail-fast TLS handshake (5s) so venue blackouts surface quickly.
	if tr.TLSHandshakeTimeout != 5*time.Second {
		t.Errorf("TLSHandshakeTimeout: want 5s got %v", tr.TLSHandshakeTimeout)
	}
	// Keep-alives must be ON (load-bearing for warm TLS).
	if tr.DisableKeepAlives {
		t.Errorf("DisableKeepAlives must be false")
	}
}

func TestNewHTTPClient_IndependentInstances(t *testing.T) {
	c1 := NewHTTPClient(5 * time.Second)
	c2 := NewHTTPClient(10 * time.Second)
	if c1 == c2 {
		t.Errorf("each call should return a fresh client")
	}
	if c1.Timeout == c2.Timeout {
		t.Errorf("independent timeouts: %v vs %v", c1.Timeout, c2.Timeout)
	}
}
