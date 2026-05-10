// Background DNS pre-resolution + TCP connection warm-up for all venue
// hostnames. Called once at process start (from main.go) — eliminates the
// DNS-lookup tax (~5-30ms per venue) on the first user-triggered call to
// any venue after a fetcher restart.
//
// We don't keep the connection open; we just prime the OS's DNS cache by
// resolving each hostname. The persistent http.Client (set up by each
// adapter's New()) then reuses warm DNS results when its first request
// fires.
package trade

import (
	"context"
	"net"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
)

// Hostnames each venue REST adapter will hit. Kept here as a flat list
// rather than reflected from each adapter's baseURL constant — those
// are private package constants and reflection here would force exposing
// them; the cost is one duplication that needs to stay in sync with
// `<venue>/<venue>.go::baseURL` (rare to change, easy to grep).
var venueHostnames = []string{
	"fapi.binance.com",       // binance
	"api.binance.com",        // binance spot
	"fapi.asterdex.com",      // aster
	"api.bybit.com",          // bybit
	"www.okx.com",            // okx
	"api.bitget.com",         // bitget
	"api.gateio.ws",          // gate
	"open-api.bingx.com",     // bingx
	"api.hyperliquid.xyz",    // hyperliquid
	"api-futures.kucoin.com", // kucoin
	"contract.mexc.com",      // mexc
	"api.backpack.exchange",  // backpack
	"mainnet.zklighter.elliot.ai", // lighter
	"whitebit.com",           // whitebit
	"futures.kraken.com",     // kraken
	"api.hbdm.com",           // htx futures
	"api.huobi.pro",          // htx spot
	"api.ethereal.trade",     // ethereal
	"api.prod.paradex.trade", // paradex
}

// PrewarmDNS resolves every venue hostname in parallel. Should be called
// once at fetcher startup. Best-effort: if a venue is unreachable from
// the host, we just log and move on — the per-call DNS lookup will surface
// the failure when an order is actually placed.
func PrewarmDNS() {
	r := net.DefaultResolver
	for _, host := range venueHostnames {
		host := host
		go func() {
			// Per-goroutine ctx — the prior `defer cancel()` in
			// PrewarmDNS() fired immediately on return, killing every
			// in-flight goroutine's DNS lookup. Now each goroutine
			// owns its own 5s timeout context.
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			t0 := time.Now()
			ips, err := r.LookupIPAddr(ctx, host)
			elapsed := time.Since(t0)
			if err != nil {
				log.L().Warn().
					Str("host", host).
					Dur("elapsed", elapsed).
					Err(err).
					Msg("dns prewarm failed")
				return
			}
			ipStrs := make([]string, 0, len(ips))
			for _, ip := range ips {
				if ip.IP.To4() != nil {
					ipStrs = append(ipStrs, ip.IP.String())
				}
			}
			log.L().Info().
				Str("host", host).
				Strs("ips", ipStrs).
				Dur("elapsed", elapsed).
				Msg("dns prewarm ok")
		}()
	}
}

// (helper for the log import to avoid unused warning if we ever strip
// hostnames to empty during refactor)
var _ = strings.TrimSpace
