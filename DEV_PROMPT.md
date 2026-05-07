# Development Prompt — Avalant Feature Sprint

> Use this document as a self-contained brief for the next dev session.
> Stack: FastAPI + Python backend, Go fetcher (go-fetcher), vanilla JS frontend (no bundler, bind-mounted).
> All context is in CLAUDE.md. Prod: `root@217.216.108.111` via `-i ~/.ssh/id_ed25519_prod`.

---

## Task 1 — Audit & fix wallet credential fields for all providers

**Goal**: every exchange/DEX in the "Add wallet" form must have exactly the right credential fields.

Check `backend/providers/exchanges/` and `backend/providers/perp_dexes/` for each provider class.
For each provider verify:
- Which fields it actually uses (`api_key`, `api_secret`, `passphrase`, `private_key`, `account_index`, `l2_private_key`, etc.)
- That the frontend wallet-creation form (`frontend/app.html` wallet modal) surfaces those exact fields
- That the backend `credentials` JSON schema matches

Known issues to check:
- **Paradex**: needs `l2_private_key` (StarkNet private key), not a standard API key pair
- **Hyperliquid**: uses `private_key` (EVM key for agent signing), no passphrase
- **Aster**: uses `api_key` + `private_key` (EIP-712 signing)
- **Ethereal**: uses `private_key` (personal_sign)
- **Lighter**: `api_key` = account_index (numeric), `api_secret` = hex private key, `passphrase` = api_key_index (default "255")
- **Extended**: read-only, no trade credentials needed
- **Backpack**: `api_key` = base64 public key, `api_secret` = base64 seed

Fix any mismatch between what the provider expects and what the form shows.
Also verify the wallet options endpoint (`GET /api/wallets/options`) returns correct `fields` metadata per venue.

---

## Task 2 — Postgres vs S3

**Already answered**: use Postgres for all transactional data (orders, positions, TP/SL, trigger orders).
S3 only if off-host log archiving or CSV export storage is added later. No action needed now.

---

## Task 3 — Fix Positions tab fetching on /arb + spot-short auto-detection

**File**: `frontend/arb.html` (bottom panel, Positions tab) + `backend/services/trade_service.py`

**Part A — Positions fetching**:
Verify that `GET /api/trade/positions` is called correctly from `/arb` and that positions from ALL venues appear, including perp DEXes (Aster, Paradex, Hyperliquid, etc.).
Check that the Positions tab renders futures AND spot positions.
If a venue is missing — trace through `trade_service.list_positions()` and the Go adapter chain.

**Part B — Spot-short auto-detection**:
Currently `list_user_spot_short_pairs()` cross-references open SHORT futures positions with spot holdings from `BalanceSnapshot.totals`.
The bug: spot LAB on Gate + short LAB on Aster is not being detected.

Root cause candidates:
1. Symbol normalization mismatch (`LAB` vs `LAB-USD` vs `LABUSDT`)
2. Aster positions not returned from `list_positions()` at all
3. Snapshot freshness check (±10 min of short open) too strict
4. `BalanceSnapshot.totals` not including Gate spot `LAB`

Fix the detection. The tolerance is ±12% notional. After fix, the pair should appear in `GET /api/trade/spot-short-pairs` and render in the arb page pair panel.

---

## Task 4 — Frontend: mobile adaptation + desktop "Funding" block

### 4A — Mobile adaptation of index.html demo cards

**File**: `frontend/index.html` + inline CSS or `frontend/design.css`

The demo/mockup cards (the exchange rate cards / screener preview section) overflow on mobile.
Fix so they stack or scroll properly at ≤768px and ≤480px.
Use `overflow-x: auto` on the card row, or switch to a single-column stacked layout below 600px.
Do not break the desktop layout.

### 4B — Desktop: add interactive demo to "Funding arbitrage without the spreadsheet tax" block

**File**: `frontend/index.html`

This section currently has only text. Add a live-looking demo widget to the right side:
- A small table showing ~5 fake funding rate rows (BTC, ETH, SOL, DOGE, PEPE) with two exchange columns and a spread column
- The spread values should animate (CSS keyframe or JS setInterval) — numbers ticking up/down slightly to feel live
- Style consistent with design.css tokens (dark surface, green accent for positive spreads, red for negative)
- No real API calls — purely cosmetic/animated fake data

---

## Task 5 — Redirect logged-in users away from /login and /register

**Files**: `frontend/login.html`, `frontend/register.html`

At the top of each page's inline `<script>`, add:
```js
if (Auth.isLoggedIn()) { location.replace('/screener'); }
```
Must run before DOMContentLoaded so there's no flash of the login form.
`Auth.isLoggedIn()` is in `frontend/auth.js`.

---

## Task 6 — Rename /app → /portfolio everywhere

**Scope**: ALL references to `/app` as a route (not `app.py` the Python file, not `app` the Docker service).

Files to update:
- `frontend/app.html` → rename file to `frontend/portfolio.html`
- `app.py` route: `serve_page("app")` → `serve_page("portfolio")` for the `/portfolio` path; add redirect `/app` → `/portfolio` (301)
- `frontend/navbar.js` — nav link href
- `frontend/index.html` — any CTA linking to `/app`
- `frontend/arb.html` — any link to `/app`
- `frontend/screener.html` — any link to `/app`
- `frontend/profile.html` — any link to `/app`
- `frontend/admin.html` / `frontend/admin-user.html` — any link
- `backend/app.py` — `_AUTH_PAGES` set: change `"app"` → `"portfolio"`; route definition
- `CLAUDE.md` — update page table
- nginx config if it has explicit `/app` location block

After rename, `/app` must 301 → `/portfolio`. Old bookmarks keep working.

---

## Task 7 — Trigger Orders + TP/SL + Active Orders tab + Position storage

This is the largest task. Read fully before starting. Ask if anything is unclear.

### 7.0 — Architecture decision: where to store

**New DB tables** (add as Alembic migration):

```sql
-- Arbitrage positions (our tracked instances)
arb_positions (
  id, user_id, kind ('long_short'|'spot_short'),
  long_exchange, long_symbol, long_wallet_id,
  short_exchange, short_symbol, short_wallet_id,
  entry_spread_pct, status ('open'|'closed'|'partial'),
  long_entry_price, short_entry_price,
  long_qty, short_qty,
  long_exit_price, short_exit_price,
  realized_pnl_usd,
  opened_at, closed_at,
  synced_externally BOOLEAN DEFAULT false,
  created_at, updated_at
)

-- Trigger / limit orders (our server-side monitoring)
arb_trigger_orders (
  id, user_id, arb_position_id (nullable FK → arb_positions),
  kind ('open'|'tp'|'sl'),
  mode ('trigger'|'tp'|'sl'),
  long_exchange, long_symbol, long_wallet_id,
  short_exchange, short_symbol, short_wallet_id,
  trigger_spread_pct FLOAT,   -- the spread % that fires this order
  quantity_usdt FLOAT,
  leverage INT,
  reduce_only BOOLEAN DEFAULT false,
  status ('pending'|'fired'|'cancelled'|'failed'),
  fired_at, error_msg,
  created_at, updated_at
)
```

**Existing `trade_positions` table** stays for individual leg tracking. `arb_positions` is the new arb-pair entity.

### 7.1 — Backend: trigger order monitoring service

**New file**: `backend/services/trigger_order_service.py`

- Runs every 5s (background task on both web replicas, Redis SETNX leader election same pattern as alert_service)
- For each pending trigger order: fetch current spread from `arbitrage.json` (or `/api/screener/long-short`) for the pair
- **Trigger open**: if current `in_pct` (accounting for the user's order size vs orderbook depth) ≥ `trigger_spread_pct` → execute market open via `trade_service.place_open_order()` for both legs
- **TP**: if arb position is open and current spread ≤ `tp_spread_pct` → close both legs (reduce only)
- **SL**: if arb position is open and current spread ≥ `sl_spread_pct` (spread widened beyond tolerance) → close both legs (reduce only)
- On fire: create `arb_positions` row, update `arb_trigger_orders.status = 'fired'`
- On failure: set status = 'failed', store error_msg

**Orderbook depth check for trigger**: when evaluating if trigger_spread is reachable for a given size, use the cached orderbook (`books.json`) to compute the effective spread at that qty — same logic as `Size` on the arb chart Entry/Exit.

### 7.2 — Backend API endpoints

Add to `backend/routes/trade.py` (or new `arb_orders.py`):

```
POST   /api/trade/arb-orders          — create trigger/TP/SL order
GET    /api/trade/arb-orders          — list user's active orders
PATCH  /api/trade/arb-orders/{id}     — update trigger spread / qty
DELETE /api/trade/arb-orders/{id}     — cancel order
GET    /api/trade/arb-positions        — list arb positions (with their orders)
POST   /api/trade/arb-positions/sync  — sync externally-opened pair into arb_positions
```

Validation on create:
- If `kind='open'` and current spread already ≥ trigger_spread → return `{"warning": "immediate_execution", "current_spread": X}` with HTTP 200 (not 4xx) so frontend can show the Ok/Cancel dialog
- TP/SL require `arb_position_id` (must be open position owned by user)
- TP/SL are always `reduce_only=true`

### 7.3 — Frontend: Live Trading block on /arb

**File**: `frontend/arb.html`

Replace the current right-side trade card with a unified **Live Trading** panel.

Layout (top to bottom):

```
[ Long exchange balances ]   [ Short exchange balances ]
─────────────────────────────────────────────────────
Toggle: [Open] [Close]
Leverage: [3x ▾]    Mode: [Market ▾] [Trigger ▾]
Quantity: [____] USDT   [0%][25%][50%][75%][100%]
─────────────────────────────────────────────────────
  (if Mode=Trigger)
  Trigger Spread: [___]%
─────────────────────────────────────────────────────
  [SL ○]  Spread SL: [___]%
  [TP ○]  Spread TP: [___]%
  [Reduce Only ○]
─────────────────────────────────────────────────────
Position Value: 0.00 USDT
Margin Used:    0.00 USDT
─────────────────────────────────────────────────────
[ Open Position ] or [ Close Position ]
```

**Balances**: show futures balance for long exchange, spot/futures balance for short exchange (depends on mode: long/short shows futures both sides; spot/short shows spot balance for short side).

**Trigger mode edge case**: when user inputs trigger spread, do a quick check against current spread from the live WS. If trigger ≤ current → show inline warning banner: "⚠ Spread is already at X% — this order may execute immediately. [Continue] [Cancel]"

**Removing per-exchange open buttons**: the existing individual "Open Long" / "Open Short" buttons per exchange card are removed. All execution goes through this block.

### 7.4 — Frontend: Active Orders tab in bottom panel

**File**: `frontend/arb.html`, bottom panel tabs

Add new tab **"Active Orders"** after "Positions":

Columns: Type | Pair | Exchange | Trigger% | Qty | Status | Created | Actions (Edit / Cancel)

- Trigger open orders: row with type = "Trigger Open"
- TP orders: row with type = "Take Profit", linked to position
- SL orders: row with type = "Stop Loss", linked to position
- Edit → opens modal pre-filled with current values, same validation as create
- Cancel → confirmation popup before DELETE

### 7.5 — Frontend: Positions tab enhancements

Each position row gains:
- TP/SL indicators (green/red badges) if set
- "Set TP/SL" button → opens the TP/SL fields inline or modal
- "Sync" button for externally-opened positions → POST `/api/trade/arb-positions/sync`
- P&L column (entry spread vs current spread)

If position is closed externally (detected via reconcile_service comparing exchange state), mark as closed and show final P&L.

### 7.6 — P&L tab

If two mirrored positions (long_position + short_position) belong to same `arb_position` row, show combined P&L = long_pnl + short_pnl + funding_pnl.

---

## Execution order

1. Task 5 (5 min — simple JS)
2. Task 6 (30 min — search+replace + redirect)
3. Task 4A (20 min — CSS)
4. Task 4B (30 min — animated demo widget)
5. Task 1 (45 min — provider audit)
6. Task 3 (1h — positions fetch + spot-short fix)
7. Task 7 (multi-session — start with DB migration, then service, then API, then frontend)

---

## Deploy commands

```bash
# Frontend only
ssh -i ~/.ssh/id_ed25519_prod root@217.216.108.111 "cd /root/wallet-monitor && ./scripts/deploy.sh frontend"

# Backend (after Python changes)
ssh -i ~/.ssh/id_ed25519_prod root@217.216.108.111 "cd /root/wallet-monitor && ./scripts/deploy.sh backend"

# Migration + backend
ssh -i ~/.ssh/id_ed25519_prod root@217.216.108.111 "cd /root/wallet-monitor && ./scripts/deploy.sh migrations"
```
