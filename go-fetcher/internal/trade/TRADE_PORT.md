# Trade-engine port: status + recipe

This document tracks which exchanges have been ported from Python
(`backend/services/trade_adapters/`) to Go (`go-fetcher/internal/trade/`)
and gives the recipe for porting the rest.

## Status

| Venue        | Python | Go reference | Tests |
|--------------|:------:|:------------:|:-----:|
| **binance**  |   ✓    |      ✓       |  11   |
| **bybit**    |   ✓    |      ✓       |   6   |
| **okx**      |   ✓    |      ✓       |   8   |
| **gate**     |   ✓    |      ✓       |   6   |
| **kucoin**   |   ✓    |      ✓       |   5   |
| **mexc**     |   ✓    |      ✓       |   5   |
| **bitget**   |   ✓    |      ✓       |   5   |
| **bingx**    |   ✓    |      ✓       |   3   |
| **htx**      |   ✓    |      ✓       |   4   |
| **whitebit** |   ✓    |      ✓       |   2   |
| **kraken**   |   ✓    |      ✓       |   3   |
| **backpack** |   ✓    |      ✓       |   4   |
| aster        |   ✓    |    (defer)   |   ·   |
| hyperliquid  |   ✓    |    (defer)   |   ·   |
| ethereal     |   ✓    |    (defer)   |   ·   |
| lighter      |   ✓    |    (defer)   |   ·   |
| paradex      | (RO)   |    (defer)   |   ·   |

**12 of 16 venues green.** Remaining 4 all need libraries we don't
already pull in:

- `aster` — EIP-712 (Aster chain). Needs `go-ethereum/crypto`.
- `hyperliquid` — Stark + EIP-712. Needs a Stark library.
- `ethereal` — EIP-712 over EVM. Same library as Aster.
- `lighter` — ZK signing via CGO (`lighter-sdk` ships per-platform
  shared libs). Would need a Go ZK proof library.
- `paradex` — read-only on Python today (paradex-py incompatible
  with Python 3.13). Skip until upstream restores support.

(RO = read-only on Python today — `paradex-py` won't load on Python 3.13.)

## How the cutover works

1. Add the Go adapter (recipe below) — adapter self-registers in `init()`.
2. Build + deploy `go-fetcher`. Adapter is now reachable at
   `/internal/trade/*` on `go-fetcher:8090`.
3. Add the venue to the prod env: `GO_TRADE_VENUES=binance,bybit`.
4. Restart `app` + `app2` (no rebuild needed — env-only).
5. Watch Order History for that venue. Any error returned by Go falls
   back to Python automatically — no need to ramp slowly.
6. After 24h clean, remove the venue from Python's `ADAPTERS` and
   delete the file.

There is no big-bang switchover: every venue is gated independently.

## Recipe — porting one venue

The Binance adapter (`binance/binance.go`) is the canonical reference.
Follow it line-for-line.

### 1. Read the Python source carefully

Open `backend/services/trade_adapters/<venue>.py`. Note:
- Symbol mapping (`BTC` → exchange-native form, e.g. `BTC-USDT-SWAP`).
- HMAC flavour. Most use SHA256 hex. OKX/KuCoin: base64. Kraken: SHA512 base64.
- Position mode (hedge vs one-way). Most are one-way; check the place_order.
- Quantity → contract conversion. Some venues (OKX, MEXC) trade in
  CONTRACTS, not coins — multiply by contract face value.
- Error code → friendly mapping (search for the `_FRIENDLY` dict).

### 2. Create the package

```bash
mkdir -p go-fetcher/internal/trade/<venue>
```

### 3. Skeleton

Each adapter file has the same shape:

```go
package <venue>

import (
    "context"
    "encoding/json"
    "net/http"
    "github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

const baseURL = "https://api.<venue>.com"

type Adapter struct {
    httpClient *http.Client
    // venue-specific caches (exchangeInfo, position-mode, ...)
}

func New() *Adapter { return &Adapter{ /* … */ } }
func init()         { trade.Register("<venue>", New()) }
func (a *Adapter) Name() string { return "<venue>" }

func (a *Adapter) PlaceOrder(ctx context.Context, c trade.Creds, r trade.OpenRequest)    (*trade.Result, error) { /* … */ }
func (a *Adapter) ClosePosition(ctx context.Context, c trade.Creds, r trade.CloseRequest) (*trade.Result, error) { /* … */ }
func (a *Adapter) SetLeverage(ctx context.Context, c trade.Creds, r trade.LeverageRequest) error { /* … */ }
func (a *Adapter) ListPositions(ctx context.Context, c trade.Creds, sym string) ([]trade.Position, error) { /* … */ }
func (a *Adapter) GetBalance(ctx context.Context, c trade.Creds) (*trade.Balance, error) { /* … */ }

var _ trade.Adapter = (*Adapter)(nil)
```

### 4. Signing helpers

Use what's in `trade/signing.go`:

- `trade.HMACHexSHA256(secret, payload)` — Binance, Bybit, MEXC, BingX, Aster
- `trade.HMACBase64SHA256(secret, payload)` — OKX, KuCoin
- `trade.HMACBase64SHA512(secret, payload)` — Kraken
- `trade.SortedFormQuery(map[string]string)` — deterministic urlencode

For exotic flavours (Bitget hex-of-base64, Hyperliquid Stark, Ethereal
EIP-712), wrap in a venue-local `_sign` helper. Don't reach into the
shared package — keep weirdness local.

### 5. Errors

Always return `*trade.Error`. Use the `errUser` / `errInternal` style
from the Binance adapter:

```go
func errUser(msg string, args ...any) *trade.Error {
    return &trade.Error{Kind: trade.KindUser, Message: fmt.Sprintf(msg, args...)}
}
```

Map venue error codes to friendly messages with a venue-local
`friendlyError(code, msg)` function. The friendly string is what
the user sees in /trade UI; raw `msg` survives in logs.

### 6. Wire into main.go

Add a blank import next to the other adapter imports:

```go
import (
    _ "github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade/binance"
    _ "github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade/<venue>"  // NEW
)
```

### 7. Tests

Mirror `binance_test.go`. At minimum:

- HMAC reference vector (use one from the venue's docs).
- Quantity rounding edge cases.
- HTTP roundtrip via `httptest.NewServer`:
  - happy path
  - exchange-rejects (verify error kind = exchange + friendly message)
  - `min_qty` / preflight rejection (no signing reached)
- `TestRegisteredViaInit` to confirm the adapter shows up in
  `trade.Lookup`.

Do NOT hit the real exchange — those tests are flaky and slow.

### 8. Cutover

Add the venue to `GO_TRADE_VENUES=...` in prod's `.env`,
`docker compose up -d app app2 go-fetcher` (no rebuild).

## Per-venue quirks to watch for

### OKX / KuCoin / Bitget — passphrase
All three add a third credential (`passphrase`). Make sure
`Creds.Passphrase` is included in the signed headers.

### Bybit — V5 unified
Endpoint family is `/v5/order/create` etc. Position-mode is per-symbol.
Symbol form: `BTCUSDT`, identical to Binance.

### MEXC — contract count
`quantity` field is in CONTRACTS, not coin units. The Python adapter
converts via `_contract_size(symbol)`. Mirror that lookup table.

### BingX — header signing
Signature goes in `X-BX-APIKEY` + `signature` query param. Read the
Python source carefully — BingX is fussy about parameter order.

### Aster — Binance fork
Wire shape ≈ Binance with minor differences in symbol set and in
some error codes. You can copy-paste binance.go and rebrand, then
adjust the diffs.

### Hyperliquid — Stark signature
Uses `private_key` from `Creds.PrivateKey` plus `Creds.Wallet` (the
Stark address). Signature is over a typed-data hash — the Python
adapter calls into `hyperliquid-py`. Best path in Go is to either
(a) pull a Stark library or (b) keep this adapter on Python until
last and dispatch via the proxy fall-through.

### Ethereal — EIP-712
Same story as Hyperliquid but Ethereum-style. Use `go-ethereum/crypto`
for the signature. Public WS isn't usable anyway, so trade is the
priority for this venue.

### Lighter — JWT-signed
Auth uses a long-lived JWT not HMAC. `Creds.APISecret` carries the
JWT directly; just attach it as `Authorization: Bearer <secret>`.

### Kraken — futures vs spot
Kraken-spot is at `api.kraken.com`, Kraken-futures at
`futures.kraken.com`. Python adapter targets futures only. Confirm
the BASE constant.

### Whitebit / Backpack / Htx — small fish
Less time-sensitive. Port last.

## Performance comparison (est.)

| Operation | Python wall-time | Go wall-time | Notes |
|---|---|---|---|
| Single open  | ~250-400 ms | ~200-300 ms | Network dominates either way |
| Pair open    | ~500-800 ms | ~250-400 ms | Real parallelism wins here |
| Pair close   | ~400-700 ms | ~200-350 ms | Same |
| HMAC sign    | ~80 µs      | ~25 µs       | 3× faster, but tiny share |

The big win is **pair open/close** where two legs ride concurrently —
Python's GIL serialises the signing+JSON parse step even under
`asyncio.gather`, while Go runs them on separate OS threads.

## Open questions

- Should we let go-fetcher own the order-history DB writes too? Today
  the Python `_log_order` / `_finalize_order` runs on the same async
  task as the adapter. If we move signing to Go, we keep DB writes in
  Python — simplest. (Decided: yes.)
- Do we need per-venue feature flags for the Go path, or is the global
  `GO_TRADE_VENUES` list enough? (Decided: list is enough; per-user
  overrides would be admin-controlled if we ever need a canary.)
