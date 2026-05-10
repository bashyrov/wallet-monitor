// Shared HTTP client config for all trade adapters.
//
// Each venue adapter previously created its own `&http.Client{Timeout:15s,
// Transport: &http.Transport{MaxIdleConnsPerHost:8, IdleConnTimeout:60s}}`.
// Two issues:
//  1. 60s IdleConnTimeout means an idle account pays a fresh TLS handshake
//     on every order placed > 60s after the last one — common for arb users
//     who fire infrequent but timing-sensitive trades.
//  2. No HTTP/2 negotiation, no TLS handshake timeout, no DialContext
//     keepalive tuning.
//
// `NewHTTPClient` provides a sane default that all adapters can switch to.
// Per-adapter Transport overrides are still possible (e.g. test mocks).
package trade

import (
	"net"
	"net/http"
	"time"
)

// NewHTTPClient builds a Transport tuned for venue REST endpoints:
//   - HTTP/2 negotiated via ALPN where the venue supports it (binance,
//     bybit, okx all do)
//   - 5min idle timeout — keeps TLS connections warm across infrequent
//     order bursts. Most venues' load balancers also have ~5min idle.
//   - Larger per-host pool (32) for arb users firing parallel legs
//   - 5s TLS handshake timeout — fail-fast on venue blackout
//   - TCP keepalive 30s — detects half-open connections from venue side
func NewHTTPClient(timeout time.Duration) *http.Client {
	return &http.Client{
		Timeout: timeout,
		Transport: &http.Transport{
			Proxy: http.ProxyFromEnvironment,
			DialContext: (&net.Dialer{
				Timeout:   3 * time.Second,
				KeepAlive: 30 * time.Second,
			}).DialContext,
			ForceAttemptHTTP2:     true,
			MaxIdleConns:          200,
			MaxIdleConnsPerHost:   32,
			MaxConnsPerHost:       64,
			IdleConnTimeout:       300 * time.Second,
			TLSHandshakeTimeout:   5 * time.Second,
			ExpectContinueTimeout: 1 * time.Second,
			DisableCompression:    false,
			DisableKeepAlives:     false,
		},
	}
}
