# Wallet Credentials Audit (Task 1, 2026-05-07)

Goal: every CEX/DEX in the "Add wallet" form must surface exactly the fields
the provider/adapter actually consumes. Trade-only providers must be flagged
correctly so the UI can collect a private key when needed.

## Findings

### CEX exchanges (12) — OK

All 12 use the standard `api_key + api_secret (+ optional api_passphrase)`
pattern. Frontend form matches backend storage matches adapter consumption.

| Venue | api_key | api_secret | passphrase | Notes |
|---|---|---|---|---|
| binance | ✓ | ✓ | — | |
| bybit | ✓ | ✓ | — | v5 HMAC |
| okx | ✓ | ✓ | ✓ | passphrase enforced via `needs_passphrase=True` |
| gate | ✓ | ✓ | — | sha512 hex |
| mexc | ✓ | ✓ | — | |
| kucoin | ✓ | ✓ | ✓ | passphrase enforced |
| bitget | ✓ | ✓ | ✓ | passphrase enforced |
| bingx | ✓ | ✓ | — | |
| whitebit | ✓ | ✓ | — | sha512 hex over base64-body |
| backpack | ✓ | ✓ | — | api_key=base64 pubkey, api_secret=base64 seed; matches code |
| kraken | ✓ | ✓ | — | sha512 raw → base64; secret is base64 |
| htx | ✓ | ✓ | — | |

The `EXCHANGE_PROVIDERS[venue].needs_passphrase` flag drives the UI passphrase
input. Verified consistent. ✅

### Perp DEX (6) — gaps

| Venue | What adapter reads | What form collects (current) | Status |
|---|---|---|---|
| **aster** | `api_key` + `api_secret` (=EVM private key) | `api_key` + `api_secret` | ✅ matches |
| **hyperliquid** | `private_key` OR `api_secret` (EVM private key for EIP-712); address | only `address` | 🚨 **gap** — trade signing fails without private key |
| **ethereal** | `private_key` OR `api_secret` (EVM key for personal_sign); address | only `address` | 🚨 **gap** — same |
| **lighter** | `api_key` (=account_index, numeric), `api_secret` (=hex pk), `api_passphrase` (=api_key_index, default "255") | only `address` | 🚨 **gap** — totally different fields needed |
| **paradex** | `l2_private_key` (StarkNet privkey) + JWT `api_token` (cached 24h, refreshed) | `address` + `api_token` (read-only) | 🚨 **gap** — l2_private_key never collected |
| **extended** | read-only (x10-python pinned to incompatible deps) | `address` only | ✅ matches by virtue of being read-only; `soon=True` flag |

### Chain wallets (15) — OK

All chains expect a single `address` field. Form matches backend matches
RPC providers. ✅

## Why the gap matters

The Python trade adapters for HL / Ethereal / Lighter / Paradex all raise
or sign-fail without their respective private keys. The Go-fetcher trade
path is identical — `creds.PrivateKey` (or `creds.APISecret` fallback) is
checked at the top of every signed action.

Today the form only collects `address`, which means:
- Read-only flows (balances, positions, funding) work because they're
  unsigned reads keyed by address.
- Any **trade action** (open / close / leverage) submitted through us
  fails with `"requires a private key"` or equivalent.

The bug is silent because the frontend `purpose` defaults to `portfolio`
(read-only) for these venues, and the `/api/trade/wallets/{id}` toggle
endpoint refuses to flip perpdex to `screener` (`api/v1/trade.py:375` —
"Trading can only be enabled on exchange wallets"). So users physically
can't have tried trading on these venues yet.

This audit + Task 3's perpdex-purpose fix are the two pieces. Together
they unblock perpdex trading **only after we add credential collection.**

## What this commit changes

**Provider-class metadata (declarative, no behaviour change yet):**

- `HyperliquidProvider.needs_private_key = True`
- `EtherealProvider.needs_private_key = True`
- `LighterProvider.needs_private_key = True`
- `LighterProvider.needs_account_index = True`
- `LighterProvider.needs_api_key_index = True`  (defaults to "255")
- `ParadexProvider.needs_l2_private_key = True`

**Endpoint exposure** — `/api/wallets/options` `perpdex_types[]` entries
now include these flags so the frontend can render the right fields per
DEX.

## What this commit does NOT change (intentional)

- `WalletCreate` schema fields — no new request fields yet.
- `wallet_service.create_wallet` storage — still only stores `{address}`
  for non-aster perpdex.
- Frontend wallet form — still only shows the address field.

These changes need a coordinated 4-file diff (schema + service + form
JS + form HTML) plus a credential-rotation thought-experiment for
existing prod wallets. Scoping that as a follow-up so the user can
verify on a test account before rollout.

## Suggested follow-up PR

1. Extend `WalletCreate` with optional `private_key`, `l2_private_key`,
   `account_index`, `api_key_index` fields.
2. Wire `wallet_service.create_wallet` to encrypt-and-store these per
   perpdex type, mapping to the names the adapters already consume:
   - HL/Ethereal: store as `api_secret` (adapter already accepts both
     names — see hyperliquid.py:109)
   - Lighter: store `api_key=account_index`, `api_secret=hex_pk`,
     `api_passphrase=api_key_index` (defaults to "255" if blank)
   - Paradex: store `private_key=l2_private_key` and keep `api_token`
     as the JWT field
3. Update the perpdex form section in `frontend/portfolio.html` to
   conditionally render:
   - Aster: address + private key (already correct via `needs_api_key`)
   - HL / Ethereal: address + private key
   - Lighter: account_index + hex_pk + api_key_index (default "255")
   - Paradex: l2_address + l2_private_key + JWT api_token
4. Migrate existing HL/Ethereal/Lighter/Paradex wallets in prod
   gracefully — likely just prompt users to re-enter on next trade
   attempt rather than auto-migrate (we don't have their PK).
5. Re-enable PATCH /api/trade/wallets/{id} for perpdex once
   credentials can be collected (drop the line 375 reject).
