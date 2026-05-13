# Provider Audit — 2026-05-13

Comprehensive sweep of all 18 venue providers across the 5 data-plane
functions. Sources: go-fetcher source, prod metrics (30-sec sample),
prod logs (3-min window), 3 parallel source-audit agents whose
findings were then HAND-VERIFIED against the actual code (many were
over-flagged; only verified items appear below).

## Coverage matrix

```
              OB-fut  OB-spot  ticks  funding  trade
aster           ✓       ·       ✓       ✓        ✓
backpack        ✓       ✓       ✓       ✓        ✓
binance         ✓       ✓       ✓       ✓        ✓
bingx           ✓       ✓       ✓       ✓        ✓
bitget          ✓       ✓¹      ✓       ✓        ✓
bybit           ✓       ✓       ✓       ✓        ✓
ethereal        ·²      ·       ✓       ✓        ✓
extended        ✓       ·       ✓       ✓        ✓
gate            ✓       ✓       ✓       ✓        ✓
htx             ✓       ✓       ✓       ✓        ✓
hyperliquid     ✓       ✓       ✓       ✓        ✓
kraken          ✓       ✓       ✓       ✓        ✓
kucoin          ✓       ✓       ✓       ✓        ✓
lighter         ✓       ·       ✓       ✓        RO³
mexc            ✓       ·       ✓       ✓        skip⁴
okx             ✓       ✓¹      ✓       ✓        ✓
paradex         ✓       ·       ✓       ✓        ✓⁵
whitebit        ✓       ✓       ✓       ✓        ✓
```

Footnotes:
1. bitget + okx spot share file with futures (NewSpot/NewFutures).
2. ethereal OB: Cloudflare 429 from our IP — connect blocked.
3. lighter trade: ZK signing requires CGO bridge; Go returns errZK,
   Python adapter handles via lighter-sdk.
4. mexc trade: explicitly skipped (v3 futures API deprecated).
5. paradex trade: code complete but never live-verified.

## Prod health snapshot (30-sec rate, 2026-05-13 ~17:50Z)

All 28 OB endpoints (15 futures + 13 spot/wrapped) flowing. Rate range:
1.9/s (hyperliquid_spot) → 1203/s (bybit). No DEAD venues. Funding files
all mtime=0s. arb/spot_arb/dex_arb files updating on schedule. Python
app + app2 logs: clean past 10 min (excluding known lighter/auth noise).

---

## Verified defects (hand-checked against source)

### P0 — verified, fixed in this branch

**OKX `GetBalance` reports available as total**
File: [go-fetcher/internal/trade/okx/okx.go:288-316](go-fetcher/internal/trade/okx/okx.go#L288)
Both `TotalUSD` and `AvailableUSD` were set from `availBal` (or
`cashBal` if avail was zero). When the account has margin locked in
open positions, the total is under-reported and the user sees only
their available cash. The fix uses `eq` / `cashBal` for total,
`availBal` for available, and surfaces `frozenBal` in `MarginUSD`.
Two new tests pin the field separation and the cashBal fallback.

### P3 — observability artifact, not user-visible after clamp

**Pipeline latency histogram unit suspicion (bybit/extended/etc.)**
The 5-min `ObserveVenueLatency` clamp landed earlier today already
filters garbage from the histogram. Root cause for bybit's elevated
avg (~11 s) and extended/kraken (10-30 s) is venue-side behaviour, not
a unit-conversion bug — a follow-up commit could log the raw ts
sample to confirm but not a correctness issue.

---

## Findings that audit agents raised but were OVER-FLAGGED

The following looked like bugs in agent output but the actual code is
correct. Listed here so the next pass doesn't re-investigate them.

1. **MEXC `ClosePosition` side**: agent claimed inverted. Verified:
   `Position.Side` per types.go means "buy=long, sell=short" (the
   side OF the position). `p.Side==Buy → close_long (4)` and
   `p.Side==Sell → close_short (2)` is correct against MEXC docs.

2. **Binance hedge-mode positionSide unconditional**: agent claimed
   always-set breaks one-way mode. Verified at line 451 the call is
   gated on `if a.isHedgeMode(ctx, creds)`. Code is correct.

3. **Bitget close semantics missing**: agent claimed PlaceOrder used
   for close. Verified at line 434: ClosePosition uses the dedicated
   `/api/v2/mix/order/close-positions` endpoint, not PlaceOrder.

4. **Bitget funding token case mismatch**: agent claimed byToken
   keyed wrong. Verified: bitget v2 ticker returns uppercase symbols;
   per-symbol task uppercases input; keys match.

5. **OKX WS volume always 0**: agent claimed MarkPrice unset at
   volume compute. Verified at lines 88-100: `last` parsed before
   `volCcy24h` within the same case block.

6. **KuCoin avg-fill price wrong on partial fills**: agent claimed
   `funds/size` mis-reports during partial fills. Verified: that's
   the realized-fills avg-price, which IS the correct semantic.

7. **OB-adapter race conditions on `books` map**: agent flagged 8
   venues. Verified: `internal/ws/adapter.go:61` explicitly mandates
   single-goroutine adapter methods. Mutexes in some adapters (gate,
   binance, backpack) exist only because those adapters spawn helper
   goroutines internally (e.g. gate's `seedREST`). Adapters without
   spawned goroutines (bybit, kraken, kucoin, okx, paradex, whitebit,
   bitget, htx) are safe under the single-goroutine contract.

---

## True backlog (deferred, real but lower-priority)

- 6 funding adapters (backpack, kraken, ethereal, lighter, paradex,
  extended) return 5-min BackstopInterval instead of the 2s contract
  in adapter.go. None of these venues update their funding rates
  faster than the 1h epoch, so 5min latency is acceptable but
  technically off-spec.

- KuCoin migration to `/contractMarket/level2:` raw stays on
  `feat/kucoin-level2-raw` branch with the drainBuffer fix; not
  re-merged to main until soak-tested in non-prod.

- Latency histogram raw-sample logging would pin the bybit/extended
  unit suspicion. Trivial to add but no user-visible payoff.

---

## Commits in this audit branch

- `fix/audit-okx-balance`: OKX GetBalance correct separation of
  total/available/margin + tests.
