# Overnight Sprint Status — 2026-05-07

**Branch**: `feat/dev-sprint-2026-05-07`
**PR**: https://github.com/bashyrov/wallet-monitor/pull/new/feat/dev-sprint-2026-05-07
**Tests**: 754 passed, 4 skipped, 0 failed
**Backend imports clean**: ✓
**Migrations**: 2 new (h1b2c3d4e5f6 perpdex purpose backfill, i2c3d4e5f6g7 arb tables)

---

## Tasks delivered (1, 3, 4A, 4B, 5, 6, 7)

### Task 5 — login/register redirects ✅
[7b33abd](https://github.com/bashyrov/wallet-monitor/commit/7b33abd)

`Auth.redirectIfAuthed` now actually redirects (was a no-op stub), validates token first to avoid the old loop, defaults to `/screener` (non-cookie-gated → no loop possible).

### Task 3 — spot-short auto-detection ✅
[b8ec2cb](https://github.com/bashyrov/wallet-monitor/commit/b8ec2cb)

Bigger fix than expected. Two compounding bugs:
1. `Wallet.wallet_type == "exchange"` filter excluded perpdex from positions/balances/reconcile (5 sites).
2. `wallet_service.create_wallet` force-set perpdex purpose to `'portfolio'` regardless of body input.

Fixes:
- Relaxed filter to `.in_(("exchange", "perpdex"))` in trade_service (4 sites), reconcile_service, user_streams supervisor.
- Perpdex wallets now default to `purpose='both'` on create (DEX private keys serve viewing AND trading by design).
- Allow PATCH purpose for perpdex.
- Migration `h1b2c3d4e5f6` backfills existing prod perpdex wallets from `portfolio` → `both`.
- 3 regression tests lock the SQL filter behaviour.

### Task 4A — mobile adaptation ✅
[1d6c5b7](https://github.com/bashyrov/wallet-monitor/commit/1d6c5b7)

Tightened mockup-card typography/padding at ≤700px and ≤480px. Dashboard table scrolls horizontally; pair chips stack on tiny screens.

### Task 4B — funding widget ✅
[e61dfb6](https://github.com/bashyrov/wallet-monitor/commit/e61dfb6)

5-row animated table on `/#why`. Spreads jitter ±0.020% every 1.4s with green/red flash + "Updated Xs ago" odometer. Honors prefers-reduced-motion. Single-column on ≤960px.

### Task 6 — /app → /portfolio rename ✅
[87dc85c](https://github.com/bashyrov/wallet-monitor/commit/87dc85c)

- `frontend/app.html` → `frontend/portfolio.html`
- `_AUTH_PAGES`: `'app'` → `'portfolio'`
- New `_LEGACY_REDIRECTS = {'app': '/portfolio'}` returns 301 with query string preserved
- Bulk-replaced all `/app` href + JS string references across HTML/JS
- `_PORTFOLIO_PATHS` keeps both for maintenance-scope coverage during the redirect window
- 4 regression tests (301, query preservation, unauth redirect, file-rename invariant)

### Task 1 — wallet credentials audit ✅ (declarative)
[bb6079b](https://github.com/bashyrov/wallet-monitor/commit/bb6079b) + [AUDIT_WALLETS.md](AUDIT_WALLETS.md)

Found gap: HL/Ethereal/Lighter/Paradex trade signing requires private keys, but the wallet form only collects `address` for these venues. Trade actions on these venues silently fail. Hidden until now because Task 3's `purpose='portfolio'` lock prevented users from ever attempting.

This commit declares the missing metadata on provider classes (`needs_private_key`, `needs_l2_private_key`, `needs_account_index`, `needs_api_key_index`) and surfaces them through `/api/wallets/options`.

**Storage-layer plumbing (WalletCreate schema fields, encrypted storage, frontend form rendering, existing-wallet migration) is the follow-up — too risky to do autonomously without per-venue live testing.** Full follow-up plan in AUDIT_WALLETS.md.

### Task 7 — Trigger orders + arb_positions ✅
[f494766](https://github.com/bashyrov/wallet-monitor/commit/f494766) (backend) + [4f4ab82](https://github.com/bashyrov/wallet-monitor/commit/4f4ab82) (frontend)

**Storage**: `arb_positions` (user-intent rollup) + `arb_trigger_orders` (server-side conditional ledger) + `trade_positions.arb_position_id` FK. Migration `i2c3d4e5f6g7`.

**Service** ([trigger_order_service.py](backend/services/trigger_order_service.py)): 1s polling loop, atomic claim-on-fire SQL (alert_service pattern — exactly-once across replicas without Redis lease), server-side size-aware effective spread via `books.json` VWAP at the trigger's portion size. Portion-based execution, infinite-fill, scheduled activation, parent/child cascade for TP/SL, partial-fill recovery (no auto-revert).

**Auto-pair**: `auto_pair_internal_legs()` — any unwrapped TradePosition pair within ±12% notional + ±10min window auto-creates an arb_position. Means anything opened through us is 100% tracked without Sync.

**API** (`backend/api/v1/arb_orders.py`): POST/GET/PATCH/DELETE `/api/trade/arb-orders`, GET `/arb-orders/history`, GET/PATCH `/arb-positions`, POST `/arb-positions/sync`.

- Immediate-execution warning on POST AND PATCH for ALL kinds (open/close/tp/sl). Returns 200 + warning, `force=true` bypasses.
- Single TP / single SL invariant per arb_position (HTTP 409 on duplicate).
- Plan-based active trigger limit (free: 3, paid: 50, unlim: ∞).

**Frontend** (skeleton):
- New "Triggers" tab in arb.html bottom panel — table with type/pair/exchange/trigger/filled/qty/mode/status, inline cancel.
- "Sync position" button right of tabs.
- Collapsible "Trigger / TP / SL" card under the per-leg trade panels with all the inputs (trigger spread, qty, portion, TP, SL, infinite-fill, reduce-only, start-time).
- Place Trigger handler with immediate-execution Confirm dialog → re-POST with force=true.
- Live spread comparison warns inline if trigger would fire on the next tick (uses existing `_liveBasisPct` helper).
- Service started from `app.py` startup hook.

**Tests**: 27 new (14 service, 13 API). Full suite 754 passes.

---

## What's NOT in this branch (deliberate)

1. **Fully redesigned arbion-style unified Live Trading panel.** The MVP keeps the existing per-leg panels intact and adds the trigger feature as a non-disruptive bottom section. The full panel rewrite (single qty input, % allocation slider, inline balances per leg, spot-short adaptation) is a deeper refactor that would touch ~500 lines of arb.html + a lot of CSS. Worth doing in a focused session with browser preview.

2. **HL/Ethereal/Lighter/Paradex private-key collection.** Audit-only. Storage plumbing is risky without per-venue live testing — see AUDIT_WALLETS.md for the follow-up checklist.

3. **External-open auto-detection in reconcile.** Spec includes it (DEV_PROMPT §7.6.B) but currently only `auto_pair_internal_legs` runs (on internal fills). External pairs still need the Sync button. Adding to reconcile is straightforward but adds load — wanted to verify the Sync flow first.

4. **WS push of position updates to frontend.** Spec says v1 = 5s poll (line 731 in DEV_PROMPT.md §7.6.D). Implemented as such; WS push deferred.

---

## Pre-deploy checklist for you

- [ ] Review AUDIT_WALLETS.md and decide on the credentials follow-up
- [ ] On a test account: create perpdex wallet, verify `purpose='both'` (was `portfolio`)
- [ ] On test account with both Gate spot LAB + Aster short LAB: hit `/api/trade/spot-short-pairs` — should now return the pair
- [ ] On a test account: open a Trigger via `/arb`, verify it appears in Triggers tab; cancel it
- [ ] Run `./scripts/deploy.sh migrations` (perpdex backfill + arb tables — runs in alembic order)
- [ ] Run `./scripts/deploy.sh backend` (rolling)
- [ ] Run `./scripts/deploy.sh frontend` (no rebuild — bind-mount)

---

## Files touched

```
M  CLAUDE.md
M  app.py
A  AUDIT_WALLETS.md
A  SPRINT_STATUS_2026-05-07.md
M  DEV_PROMPT.md
A  alembic/versions/h1b2c3d4e5f6_perpdex_purpose_both.py
A  alembic/versions/i2c3d4e5f6g7_arb_positions_triggers.py
A  backend/api/v1/arb_orders.py
M  backend/api/v1/router.py
M  backend/api/v1/wallets.py
M  backend/db/models.py
M  backend/providers/perp_dexes/ethereal_provider.py
M  backend/providers/perp_dexes/hyperliquid_provider.py
M  backend/providers/perp_dexes/lighter_provider.py
M  backend/providers/perp_dexes/paradex_provider.py
M  backend/services/reconcile_service.py
M  backend/services/trade_service.py
A  backend/services/trigger_order_service.py
M  backend/services/user_streams/_supervisor.py
M  backend/services/wallet_service.py
M  frontend/arb.css
M  frontend/arb.html
RM frontend/app.html → frontend/portfolio.html
M  frontend/auth.js
M  frontend/index.html
M  frontend/login.html
M  frontend/navbar.js
M  frontend/register.html
+ all other frontend HTML files (bulk /app → /portfolio rename)
A  tests/test_arb_orders_api.py
A  tests/test_perpdex_credentials_audit.py
A  tests/test_perpdex_position_filter.py
A  tests/test_portfolio_route.py
A  tests/test_trigger_orders.py
```

10 commits on the branch.
