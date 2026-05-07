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

**Status**: root cause located by code audit (2026-05-07). One-line fix + 3 verifications.

### Root cause

`backend/services/trade_service.py:577` — `_list_user_positions_inner()` filters wallets with `Wallet.wallet_type == "exchange"`. Aster / Hyperliquid / Paradex / Ethereal / Lighter / Extended are `wallet_type='perpdex'` → their short positions are **never queried**. Spot side is fine: `_list_user_spot_holdings()` at line 1468 already uses `.in_(("exchange", "perpdex"))`.

Symbol normalization is **correct**:
- Gate spot: `BalanceSnapshot.totals` keys are uppercase asset symbols (`"LAB"`)
- Aster short: [aster.py:222](backend/services/trade_adapters/aster.py#L222) strips `"USDT"` → `"LAB"`

So spot LAB on Gate is found, but no short LAB on Aster ever reaches the pairing logic — pair list is empty.

### Fix

```python
# backend/services/trade_service.py:577
# BEFORE
Wallet.wallet_type == "exchange"
# AFTER
Wallet.wallet_type.in_(("exchange", "perpdex"))
```

Same edit anywhere else `list_positions` / `list_orders` / `list_balances` filter to `"exchange"` only — grep for `wallet_type == "exchange"` across `backend/services/`.

### Pre-merge verifications

1. **Stale `unpaired` decisions**: `SELECT * FROM trade_pair_decisions WHERE decision='unpaired' AND (leg_a_key LIKE 'LAB|%' OR leg_b_key LIKE 'LAB|%');` — if any rows match the user's pair, instruct them to "Repair" via UI (or wipe row).
2. **Wallet `purpose`**: Aster wallet must have `purpose IN ('portfolio','both')` — `purpose='screener'` excludes it from trade flows. Check via `/admin → Users → wallets`.
3. **Live test**: with Aster (short LAB) + Gate (spot LAB) credentials, hit `GET /api/trade/positions` and `GET /api/trade/spot-short-pairs`. Aster row must appear in positions; auto-paired entry must appear in spot-short-pairs.

### Out-of-scope (do NOT touch)

- ±12% notional tolerance — keep (regression risk)
- ±10min freshness — keep (already correct, candidates surface for manual pairing on miss)
- Symbol normalization — works as-is

**Effort**: 30 min including local test. Deploy: `./scripts/deploy.sh backend`.

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

## Task 7 — Trigger Orders + TP/SL + Active Orders + arb_positions

> Largest task. Read fully before starting. Reference UX: arbion.trade/spot (their tagline confirms triggers are core: *"Trade USDT-Futures & Spot Arbitrage by using triggers"*).

**Estimated effort**: ~24h chunk. Suggested breakdown across 3 sessions.

---

### 7.0 — Storage model: arb_positions vs trade_positions

`trade_positions` already exists with `kind='pair'` grouping two legs. **Don't replace it.** Instead:

- **`trade_positions`** stays as the *execution ledger* — every leg, every fill, raw venue state. Reconcile-service authoritative.
- **`arb_positions`** is the new *user-intent entity* — "the arb I want to track and attach TP/SL to". One arb_position can wrap 1..N trade_positions over its lifetime (e.g., partial close + re-open creates a second trade_positions row under the same arb_position).

Relationship:

```
arb_positions (1) ←─ (1..N) trade_positions    via trade_positions.arb_position_id (NEW FK)
arb_positions (1) ←─ (0..N) arb_trigger_orders via arb_trigger_orders.arb_position_id
```

This means the migration touches 3 things: create 2 new tables + add a nullable FK column to `trade_positions`.

### 7.1 — Migration (Alembic)

Schema reflects arbion's UX: portion-based execution (chunked fills), optional infinite fill, scheduled activation, TP **and** SL as linked child triggers.

```sql
-- arb_positions: user-intent arb pair tracking
CREATE TABLE arb_positions (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    kind VARCHAR(16) NOT NULL,          -- 'long_short' | 'spot_short'
    long_exchange VARCHAR(32) NOT NULL,
    long_symbol VARCHAR(64) NOT NULL,
    long_wallet_id BIGINT REFERENCES wallets(id) ON DELETE SET NULL,
    short_exchange VARCHAR(32) NOT NULL,
    short_symbol VARCHAR(64) NOT NULL,
    short_wallet_id BIGINT REFERENCES wallets(id) ON DELETE SET NULL,

    -- intent / sizing
    target_qty_token FLOAT,             -- intended total qty in token units (e.g. 5000 VANRY)
    leverage INTEGER,                   -- intended leverage on perp leg(s); ignored for spot leg
    margin_mode VARCHAR(8) DEFAULT 'isolated',  -- 'isolated' | 'cross'

    -- entry (filled in once both legs open / on each portion fill)
    entry_spread_pct FLOAT,             -- VWAP-weighted across all filled portions
    long_entry_price FLOAT,
    short_entry_price FLOAT,
    long_qty FLOAT,                     -- accumulated filled qty
    short_qty FLOAT,
    opened_at TIMESTAMP,

    -- exit (filled in on close)
    exit_spread_pct FLOAT,
    long_exit_price FLOAT,
    short_exit_price FLOAT,
    realized_pnl_usd FLOAT,
    closed_at TIMESTAMP,

    -- state
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    -- 'pending'   = trigger created, not fired
    -- 'opening'   = first fire in progress
    -- 'open'      = at least one portion filled both legs
    -- 'partial'   = one leg has fills, other missing — needs user attention
    -- 'closing'   = close in progress
    -- 'closed'    = both legs fully closed
    -- 'cancelled' = trigger cancelled before firing

    synced_externally BOOLEAN NOT NULL DEFAULT false,
    closed_externally BOOLEAN NOT NULL DEFAULT false,

    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_arb_positions_user_status ON arb_positions(user_id, status);
CREATE INDEX ix_arb_positions_opened ON arb_positions(opened_at);

-- arb_trigger_orders: server-side conditional orders (open/tp/sl)
-- Note: TP/SL rows are created together with their parent open trigger,
-- linked via arb_position_id (set after the open fires its first portion).
CREATE TABLE arb_trigger_orders (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    arb_position_id BIGINT REFERENCES arb_positions(id) ON DELETE CASCADE,
    parent_trigger_id BIGINT REFERENCES arb_trigger_orders(id) ON DELETE CASCADE,
    -- parent_trigger_id: TP/SL point to their parent 'open' trigger
    -- so cancellation of the parent cascades.

    kind VARCHAR(16) NOT NULL,            -- 'open' | 'close' | 'tp' | 'sl'
    -- 'open'  fires when current effective spread >= trigger_spread_pct
    -- 'close' fires when current effective spread <= trigger_spread_pct (manual close trigger)
    -- 'tp'    fires when current effective spread <= trigger_spread_pct (linked to open position)
    -- 'sl'    fires when current effective spread >= trigger_spread_pct (linked to open position)

    -- trigger condition (always absolute % — no relative mode in v1)
    trigger_spread_pct FLOAT,            -- NULL means "fire at next tick" (= market)

    -- pair identity (for 'open' kind; child kinds inherit from arb_position)
    long_exchange VARCHAR(32),
    long_symbol VARCHAR(64),
    long_wallet_id BIGINT REFERENCES wallets(id) ON DELETE SET NULL,
    short_exchange VARCHAR(32),
    short_symbol VARCHAR(64),
    short_wallet_id BIGINT REFERENCES wallets(id) ON DELETE SET NULL,

    -- order params
    total_qty_token FLOAT,                -- total intended qty (token units)
    portion_size_token FLOAT,             -- chunk size per fire; if NULL, fire whole qty in one shot
    portions_filled INTEGER NOT NULL DEFAULT 0,
    portions_target INTEGER,              -- ceil(total_qty / portion_size); NULL if portion_size NULL
    infinite_fill BOOLEAN NOT NULL DEFAULT false,
    -- infinite_fill=true: re-arm to 'pending' after each portion until cancelled
    --                    (used for grid-style arb collection through the funding window)
    -- infinite_fill=false: status='fired' once portions_filled == portions_target
    activate_at TIMESTAMP,                -- if set, service ignores until NOW() >= activate_at

    leverage INTEGER,
    margin_mode VARCHAR(8) DEFAULT 'isolated',
    reduce_only BOOLEAN NOT NULL DEFAULT false,

    -- state machine (per row)
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    -- 'scheduled'  = activate_at in future
    -- 'pending'    = waiting for spread condition
    -- 'firing'     = atomic claim won, executing now (~ms)
    -- 'fired'      = portions_filled == portions_target (or cancelled-after-partial-fills)
    -- 'failed'     = execution attempted, ≥1 leg rejected
    -- 'cancelled'  = user cancelled

    last_fired_at TIMESTAMP,              -- of most recent portion fire
    error_kind VARCHAR(16),               -- 'exchange' | 'internal' | 'user' | 'partial'
    error_msg TEXT,

    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_arb_trigger_orders_status ON arb_trigger_orders(status);
CREATE INDEX ix_arb_trigger_orders_user ON arb_trigger_orders(user_id);
CREATE INDEX ix_arb_trigger_orders_position ON arb_trigger_orders(arb_position_id);
CREATE INDEX ix_arb_trigger_orders_parent ON arb_trigger_orders(parent_trigger_id);
CREATE INDEX ix_arb_trigger_orders_activate ON arb_trigger_orders(activate_at)
    WHERE activate_at IS NOT NULL;

-- Wire trade_positions to its parent arb_position
ALTER TABLE trade_positions
    ADD COLUMN arb_position_id BIGINT REFERENCES arb_positions(id) ON DELETE SET NULL;
CREATE INDEX ix_trade_positions_arb ON trade_positions(arb_position_id);
```

**SQLAlchemy models** in `backend/db/models.py` — `ArbPosition`, `ArbTriggerOrder` with relationship backrefs (`children` for parent→TP/SL, `parent` reverse).

### 7.2 — Trigger monitoring service

**New file**: `backend/services/trigger_order_service.py`

**Cadence**: 1s loop. (DEV_PROMPT originally said 5s — too coarse. Funding spreads flicker on 200ms scale; 5s misses flash entries.)

**Concurrency**: **atomic claim-on-fire SQL** (alert_service pattern), NOT Redis lease. Reasoning:
- Lease pattern has a 60s race window if leader crashes mid-fire → second replica re-fires the same trigger → duplicate order.
- Atomic UPDATE…WHERE locks the row in Postgres; only one replica wins per trigger. No race possible. No Redis dependency.
- Both web replicas (app + app2) run the loop concurrently, sharing the same DB.

**Pseudocode**:

```python
async def _tick():
    # Promote scheduled → pending if activate_at reached
    db.execute(
        "UPDATE arb_trigger_orders "
        "SET status='pending', updated_at=NOW() "
        "WHERE status='scheduled' AND activate_at <= NOW()"
    )

    pending = db.query(ArbTriggerOrder).filter_by(status='pending').all()
    for order in pending:
        spread = await _effective_spread(order)   # VWAP at portion_size_token
        if spread is None:        # books too stale (>5s) → skip, retry next tick
            continue
        if not _condition_met(order, spread):
            continue
        # ATOMIC CLAIM — exactly-once across replicas
        rows = db.execute(
            "UPDATE arb_trigger_orders "
            "SET status='firing', updated_at=NOW() "
            "WHERE id=:id AND status='pending' "
            "RETURNING id",
            id=order.id
        ).rowcount
        if rows == 0:             # another replica claimed it first
            continue
        await _execute_portion(order)

async def _execute_portion(order):
    """Fire ONE portion. On success, increment portions_filled.
    Re-arm to 'pending' if more portions remain or infinite_fill=true.
    """
    qty = order.portion_size_token or order.total_qty_token
    try:
        result = await _place_pair(order, qty)   # asyncio.gather both legs
    except PartialFillError as e:
        # one leg succeeded — DO NOT auto-revert
        order.status = 'failed'
        order.error_kind = 'partial'
        order.error_msg = str(e)
        await tg_alert(order.user_id, f"Partial fill on {order.long_symbol}: {e}")
        return

    order.portions_filled += 1
    order.last_fired_at = now()
    _accumulate_position(order, result)   # updates arb_positions long_qty/short_qty/entry_spread

    if order.infinite_fill:
        order.status = 'pending'                 # re-arm forever
    elif order.portions_filled >= (order.portions_target or 1):
        order.status = 'fired'                   # all portions filled
        if order.kind == 'open':
            # Activate any linked TP/SL children now that position is open
            db.execute(
                "UPDATE arb_trigger_orders "
                "SET status='pending' "
                "WHERE parent_trigger_id=:p AND status='scheduled'",
                p=order.id
            )
    else:
        order.status = 'pending'                 # more portions to go

async def _effective_spread(order):
    """Read books.json (Go-fetcher cache). VWAP at portion_size_token.
    Reuse arb.html:_vwap() logic (line 2786-2801) ported to Python.
    Return None if either leg's book is >5s old.
    """
    ...

def _condition_met(order, spread):
    # All conditions are absolute spread thresholds (v1 — no relative mode).
    if order.trigger_spread_pct is None:
        return True                              # market trigger, fire next tick
    if order.kind in ('open', 'sl'):
        return spread >= order.trigger_spread_pct  # spread widened past threshold
    elif order.kind in ('close', 'tp'):
        return spread <= order.trigger_spread_pct  # spread converged below threshold
```

**Lifecycle scenarios**:

| Scenario | Behaviour |
|---|---|
| Single-shot open (no portions) | `portion_size_token=NULL`, `portions_target=1`. One fire, fills full qty, status → `fired`. |
| Portioned open | `portion_size_token=100`, `total_qty_token=1000` → `portions_target=10`. Each tick where condition met fires one portion until 10 filled. |
| Infinite fill | `infinite_fill=true`. Re-arms to `pending` forever; user cancels via UI to stop. Useful for collecting funding through the entire window. |
| Open with linked TP+SL | Backend creates 3 rows in one transaction: parent `open`, child `tp` (status='scheduled', parent_trigger_id=open.id), child `sl` (status='scheduled', parent_trigger_id=open.id). After parent fills its first portion → children promoted to `pending`. |
| Scheduled start | `activate_at='2026-05-08 14:00:00'` (e.g., 5 min before funding settlement). Service wakes them on schedule. |
| Partial fill of one portion | Leg A filled, leg B rejected → `arb_position.status='partial'`, trigger → `failed`, TG alert. **No auto-revert** — user decides. |
| TP fires after position open | Closes ONLY the qty held in arb_position (not the full TP target_qty if portions still pending). Cancels remaining open-trigger portions. |

**`trade_delay_ms` is NOT bypassed**. Free users get 500ms latency per leg even on auto-fire. `asyncio.gather()` parallelizes so it's max(leg_a_delay, leg_b_delay), not sum.

### 7.3 — API endpoints

Mount under `backend/api/v1/trade.py` (existing file; add at bottom):

```
POST   /api/trade/arb-orders          create trigger/TP/SL
GET    /api/trade/arb-orders          list active (status IN pending/firing)
GET    /api/trade/arb-orders/history  list closed (status IN fired/failed/cancelled), paginated
PATCH  /api/trade/arb-orders/{id}     update trigger params (only if status='pending')
DELETE /api/trade/arb-orders/{id}     cancel (only if status='pending')

GET    /api/trade/arb-positions       list user's arb_positions + nested orders
POST   /api/trade/arb-positions/sync  scan venue state, create arb_position rows for externally-opened pairs
PATCH  /api/trade/arb-positions/{id}  attach TP/SL to existing arb_position (creates 2 trigger_orders rows)
```

**`POST /api/trade/arb-orders` request schema**:

```json
{
  "kind": "open",                            // 'open' | 'close'
  "pair_kind": "long_short",                 // 'long_short' | 'spot_short'
  "long_exchange": "gate",
  "long_symbol": "VANRY",
  "long_wallet_id": 42,
  "short_exchange": "mexc",
  "short_symbol": "VANRY",
  "short_wallet_id": 17,

  "trigger_spread_pct": 1.5,                 // null = "Last %" = market (fire next tick)
  "total_qty_token": 5000,                   // total intended qty in token units
  "portion_size_token": 500,                 // chunk size; null = single-shot
  "infinite_fill": false,
  "activate_at": null,                       // ISO timestamp or null for immediate

  "leverage": 3,                             // ignored on spot leg in spot_short
  "margin_mode": "isolated",                 // 'isolated' | 'cross' (perp leg only)
  "reduce_only": false,

  "tp": {                                    // optional, creates linked child trigger
    "trigger_spread_pct": 0.3,
    "portion_size_token": null               // null = close full qty
  },
  "sl": {                                    // optional, creates linked child trigger
    "trigger_spread_pct": 2.5,
    "portion_size_token": null
  }
}
```

**Sizing rule**: `total_qty_token` is the source of truth. The frontend `% Allocation` slider computes `total_qty_token` from balance × allocation × leverage / mark_price. The "Quantity" input is just an alternative way to enter the same field. `portion_size_token` is independent (defaults to `total_qty_token` if Portion Size unchecked).

**Response on immediate-execution case** (current spread already meets trigger).

Applies to **POST** (create) and **PATCH** (edit) on **every trigger kind** — open, close, tp, sl. Example: user sets open trigger at 20% but current spread is 25% → would fire immediately. Or edits an existing TP from 0.3% to 0.6% while spread is already at 0.5% → would fire immediately. In every case:

```http
HTTP 200 OK
{
  "warning": "immediate_execution",
  "kind": "tp",                                // which trigger kind would fire
  "current_spread": 0.50,
  "requested_trigger": 0.60,
  "draft_id": "tmp_xyz123"                      // POST only; PATCH echoes the existing id
}
```

Frontend pops **Ok / Cancel** modal:
- **Cancel** → drop the request, let the user adjust the value. Nothing changes server-side.
- **Ok** → re-POST (or re-PATCH) with `force=true`. Backend skips the check, persists, trigger fires on the next tick (< 1s).

Skipped entirely when `trigger_spread_pct=null` (user explicitly chose `Last %` = market) — that's an explicit "fire now" intent, no warning needed.

For TP/SL nested in the parent open `tp:{}`/`sl:{}` payload, the warning surfaces per child with `kind` set to which one would fire immediately (open, tp, sl can each fire on the next tick if their threshold is already met against current spread).

**Validation rules**:
- `kind='open'` requires venues + symbol + total_qty_token.
- `kind='close'` requires `arb_position_id` (must be owned by user, status IN ('open','partial')). `reduce_only` auto-set to `true` regardless of payload.
- `kind='tp'` and `kind='sl'` (or nested TP/SL on a parent open):
  - Auto-set `reduce_only=true` regardless of payload — TP/SL are *always* reduce-only.
  - Exactly one active TP and one active SL per `arb_position` at a time. Backend rejects with HTTP 409 `{"error": "tp_already_exists"}` if user tries to add a 2nd TP. (To replace, user PATCHes the existing one or DELETEs it first.)
  - TP `trigger_spread_pct` must be ≤ current spread (otherwise immediate-execution warning).
  - SL `trigger_spread_pct` must be ≥ current spread (otherwise immediate-execution warning).
- `pair_kind='spot_short'`: `leverage` and `margin_mode` are accepted but apply only to the short (perp) leg. Long leg is spot — server ignores leverage for that leg.
- `portion_size_token` ≤ `total_qty_token` if both set.
- `infinite_fill=true` requires `portion_size_token` set (else nonsensical).
- Plan-based limit on max active triggers (Free: 3, paid: 50, Unlim: ∞) — config in `plans.features` JSON.

### 7.4 — Frontend: Live Trading panel on /arb (long-short and spot-short)

**File**: `frontend/arb.html` — replace the current per-leg trade card (lines 560–691) with a unified panel.

**Reference**: arbion.trade/spot screens (provided 2026-05-07). We match their input model (Trigger Spread always shown with `Last %` placeholder, Portion Size + Take Profit + Infinite Fill + Reduce Only + Start Time as toggleable sections) but adapt for our requirements:

| arbion | us |
|---|---|
| no inline balances (logged-out demo) | **inline balances per leg** (top of panel) |
| Isolated only | **Isolated + Cross** (both available) |
| Open / Close / Infinity+ | **Open / Close** (Infinity+ deferred to v1.1) |
| TP only on Open form | **TP and SL** both on Open form, as linked child triggers |
| Reduce Only on Open | **Reduce Only on Open and Close** |

**Mode detection**: `pair_kind` is determined by URL/route — `/arb` ⇒ `long_short`, `/spot-arb` (or context with spot wallet on long leg) ⇒ `spot_short`. Panel re-renders affected controls accordingly.

**Layout — Open tab (long-short)**:

```
┌─ Live Trading ────────────────────────────── [Keys ⚙] ─┐
│                                                         │
│  ┌─ Long: Gate ────────┐    ┌─ Short: MEXC ────────┐   │
│  │  Bal:  1,243.50 USDT │    │  Bal:  876.20 USDT    │   │
│  │  Avail: 821.40       │    │  Avail: 712.10        │   │
│  └──────────────────────┘    └───────────────────────┘   │
│                                                          │
│  [● Open]  [○ Close]                                     │
│                                                          │
│  Margin: [Isolated ▾]      Leverage: [3x ▾]              │
│                                                          │
│  ┌─ Trigger Spread ───────────────────────────────────┐ │
│  │  [_______________________________] Last  %         │ │
│  │  ⚠ current spread is 1.62% — would fire next tick  │ │
│  └────────────────────────────────────────────────────┘ │
│                                                          │
│  Quantity:    [______________] VANRY                     │
│                                                          │
│  % Allocation                                       0%   │
│  ●━━━━━○━━━━━━○━━━━━━○━━━━━━○                            │
│  0%    25%    50%    75%    100%                         │
│                                                          │
│  Position Value:  0.00 USDT                              │
│  Margin Used:     0.00 USDT                              │
│  Effective spread @ size:  0.42%                         │
│                                                          │
│  [☐] Portion Size                                        │
│      Portion Size: [_________] VANRY                     │
│      One Portion Cost: 0.00 USDT                         │
│                                                          │
│  [☐] Take Profit                                         │
│      Trigger Spread: [_______]  %                        │
│      Portion Size:   [_______]  VANRY  (blank = full)    │
│                                                          │
│  [☐] Stop Loss                                           │
│      Trigger Spread: [_______]  %                        │
│      Portion Size:   [_______]  VANRY  (blank = full)    │
│                                                          │
│  [☐] Infinite Fill                                       │
│  [☐] Reduce Only                                         │
│  [☐] Start Time     [2026-05-08 14:00 ▾]                 │
│                                                          │
│  [          Place Trigger / Open Now           ]         │
└──────────────────────────────────────────────────────────┘
```

**Layout — Close tab**: same as Open but:
- Sizing controls bound to current open position qty (max = position qty).
- `% Allocation` slider becomes "% of position".
- `Margin` and `Leverage` selects hidden (locked from position).
- `Take Profit` / `Stop Loss` sections hidden (those are open-time choices).
- `Reduce Only` checkbox is shown and **defaults to checked** (you generally want close to be reduce-only). User can uncheck.
- Button label: **Place Close Trigger / Close Now**.

**Layout — Spot-short adaptation** (`pair_kind='spot_short'`):

The long leg is spot, the short leg is perp. The form re-renders:

- **Long-side balance card** shows spot asset balance (e.g., `Avail: 1,234 USDT` if buying with USDT, plus the resulting token holdings).
- **Margin** select hides for the long leg (spot has no margin mode). Stays for short leg only.
- **Leverage** select applies only to the short leg (label changes: `Leverage (short): 3x`).
- **Reduce Only** stays — applies to short leg close.
- **Take Profit / Stop Loss**: same logic. On TP/SL fire, long leg is sold at market (spot), short leg is closed (perp reduce-only).
- **Quantity** input remains in token units; sizing math accounts for spot fill (no leverage multiplier on long side).

**Behaviour notes**:

- **Trigger Spread placeholder `Last %`**: empty input means "fire at the next tick at current spread" (= effective market order). No separate Market/Trigger mode toggle.
- **Effective spread @ size**: live-computed via `_vwap()` from book WS (already exists at [arb.html:2786](frontend/arb.html#L2786)). Surfaces the actual fillable spread for the entered Portion Size (or full qty if no portions). Re-uses the same chart-size selector logic that already drives Entry/Exit candles.
- **Immediate-execution warning**: when user types a non-empty Trigger Spread, if condition is already met by current effective spread → inline yellow banner. Submit button label flips to **"Place Trigger (fires now)"**. Skipped when `Trigger Spread` is empty (user explicitly chose market).
- **% Allocation** slider: dynamically computes `total_qty_token` = `availableBalance × allocation × leverage / markPrice` (long-short) or `= spotAvail × allocation / markPrice` (spot-short long leg). Quantity input updates in lockstep.
- **TP/SL checkboxes**: when checked, expose `Trigger Spread %` and `Portion Size` fields. On submit, backend creates 1 parent open trigger + N linked child triggers (TP and/or SL). Children sit in `status='scheduled'` until parent's first portion fires; then promoted to `pending`.
- **Portion Size** unchecked → single-shot fill. Checked → portioned (each chunk fires when condition met).
- **Infinite Fill** requires Portion Size checked. Re-arms the trigger after every portion until user cancels (or funding window closes — manual stop).
- **Start Time** — date/time picker. Trigger sits in `status='scheduled'` until then.
- **Balances**: pull from `/api/trade/balances`, refresh every 5s. Use existing `_accFetchBalances()` plumbing.

**Removed from existing trade card** (lines 569–622, 630–683 in [arb.html](frontend/arb.html)):
- Per-exchange "Open Long" / "Open Short" buttons.
- "Open Both" button (line 625).
- Per-leg leverage steppers (replaced by single Margin/Leverage row at panel-level for long-short; per-leg only for spot-short).

All execution now goes through the unified panel.

### 7.5 — Triggers tab (bottom panel)

Renamed from "Active Orders" to **Triggers** to match arbion convention. New tab inserted between Positions and Order History:

```
┌─ Tabs: [Positions] [Triggers] [Order History] [P&L] [Balances] ──┐
                                                                    │
                              [☐ Show only spot]   [+ Sync position]│
```

The "Sync position" button (top-right of the bottom panel) calls `POST /api/trade/arb-positions/sync` — same UX as arbion's «Синхронізувати позицію». Sync scans both venues for matching open positions and wraps them in `arb_position` rows so TP/SL can be attached.

**Triggers columns**:

| Type | Pair | Long → Short | Trigger | Filled | Qty | Mode | Status | Created | Actions |
|---|---|---|---|---|---|---|---|---|---|
| Open       | VANRY | gate → mexc  | ≥ 1.50%  | 3 / 10  | 5000 VANRY | portion 500, ∞-off | pending | 12:34 | Edit / ✕ |
| Take Profit| VANRY | gate → mexc  | ≤ 0.30%  | 0 / 1   | 5000 VANRY | full close      | scheduled | 12:34 | ✕ |
| Stop Loss  | VANRY | gate → mexc  | ≥ 2.50%  | 0 / 1   | 5000 VANRY | full close      | scheduled | 12:34 | ✕ |
| Close      | LAB   | gate → aster | ≤ 0.10%  | 0 / 1   | 1200 LAB   | reduce-only     | pending | 11:02 | Edit / ✕ |

- **Filled column**: shows `portions_filled / portions_target`. For infinite-fill, shows `N / ∞`.
- **Mode column**: free-form summary — portion size, infinite fill marker, reduce-only flag.
- **Status badges**: `scheduled` (gray, with activate_at tooltip), `pending` (blue), `firing` (yellow, animated), `fired` (green), `failed` (red, click for error_msg), `cancelled` (gray strike-through).
- **Edit** opens modal pre-filled with current values (only `pending`/`scheduled` rows; firing locks). Same validation as create. Cancelling a parent cascades to its children (TP/SL siblings).
- **✕ Cancel** uses `Confirm.ask()` (existing helper [confirm.js]) before DELETE.
- Auto-refresh every 3s via `/api/trade/arb-orders`. Status changes animate with subtle color flash.
- History sub-tab (toggle): fired/failed/cancelled rows from `/api/trade/arb-orders/history`, paginated 50.

**Spot-only filter**: the `Show only spot` checkbox filters Positions tab to spot legs (mirrors arbion's «Показати лише спот»). Useful when user has both spot and perp positions on the same venue.

### 7.6 — Position tracking & reconciliation

Position state has three sources of truth that must converge:

1. **Venue** — authoritative ground truth (positions API + balance API)
2. **`trade_positions`** — execution ledger (every fill, raw)
3. **`arb_positions`** — user-intent rollup (what the UI shows as "my arb")

The flow below keeps all three in sync.

**A. Accumulation during fills**

When `trigger_order_service._execute_portion()` lands a successful fill, it folds the result into the parent `arb_position`:

```python
def _accumulate_position(trigger, fill):
    pos = trigger.arb_position or _create_arb_position(trigger)

    # VWAP-merge entry prices across all filled portions so far
    pos.long_entry_price  = vwap_merge(pos.long_entry_price,  pos.long_qty,
                                       fill.long_price,       fill.long_qty)
    pos.short_entry_price = vwap_merge(pos.short_entry_price, pos.short_qty,
                                       fill.short_price,      fill.short_qty)
    pos.long_qty  += fill.long_qty
    pos.short_qty += fill.short_qty
    pos.entry_spread_pct = (pos.short_entry_price - pos.long_entry_price) \
                           / pos.long_entry_price * 100.0

    if pos.opened_at is None:
        pos.opened_at = now()
    pos.status = 'open'

    # Wire the per-leg trade_positions row to this arb_position
    fill.long_trade_position.arb_position_id  = pos.id
    fill.short_trade_position.arb_position_id = pos.id
```

VWAP-merge means entry_spread reflects the *true average* across all filled portions, not just the first. This is critical for honest P&L when the user uses Portion Size or Infinite Fill.

**B. Reconcile loop (extends existing `reconcile_service`)**

`backend/services/reconcile_service.py` already runs hourly and syncs `trade_positions` with venue state. Add a new pass after the existing logic that walks `arb_positions WHERE status IN ('open','partial','closing')`:

| Detection | Action |
|---|---|
| Venue has 0 qty on **both** legs, our status='open' | **Closed externally.** Pull final data from venue: `long_exit_price`, `short_exit_price`, `closed_at`, leg-level `realized_pnl_usd`, accumulated `funding_pnl_usd`. Set `closed_externally=true`, `status='closed'`. Compute `arb_position.realized_pnl_usd = leg_a + leg_b + funding`. **Cascade-cancel** all `pending`/`scheduled` TP/SL children with `error_kind='position_closed_externally'`. The pair shows up in the P&L tab with summed two-leg P&L. |
| Venue qty < our qty (one or both legs) | **Partial external close.** Pull the venue's incremental fills, attribute to our `trade_positions` (closes against existing legs FIFO). Reduce `pos.long_qty` / `pos.short_qty` to match venue. If a leg hits 0 → `status='partial'`. TP/SL stay active on remaining qty. |
| Venue qty > our qty | User added to the position outside our system. Update `long_qty` / `short_qty` to match venue, recompute `entry_spread_pct` via VWAP-merge with venue's incremental fills. Set `synced_externally=true` (UI flags the row). |
| Both venues report no position **and** we never recorded one | Skip — empty arb_position row from a cancelled trigger; leave it `cancelled`/`closed`. |

**Cadence**: 5 min for arb_positions specifically (separate task in `reconcile_service`). The hourly `trade_positions` reconcile is too slow for the UI; 5 min matches expected user reaction time.

**External open detection is OUT of reconcile** — too noisy (false positives when user opens unrelated positions). Handled exclusively by the manual `Sync` button (G below).

**C. Spot-short asymmetry**

Spot has no "open position" concept — it's a balance delta. Tracking adapts:

- **Long leg (spot)**: source-of-truth is `BalanceSnapshot.totals[asset]`. After each portion fill, expected balance = `pre_fill_balance + accumulated_long_qty`. Reconcile compares fresh snapshot to expected.
- **Short leg (perp)**: standard position API, same as long-short.
- **External spot close**: user sells the asset off-platform. Detected when `BalanceSnapshot.totals[asset] < expected`. If only spot reduced → `status='partial'`.
- **Reconcile pre-step**: for any open spot-short arb_position, force-refresh the spot venue's `BalanceSnapshot` at the start of the cycle (skip the "fresh enough" cache). Without this, stale snapshots produce false partials.

**D. Live updates to frontend**

**v1: 5s poll** of `GET /api/trade/arb-positions` from the Positions tab. Simple, matches arbion, no WS plumbing.

Skip WS push for v1 unless feedback demands it. If we add it later: piggyback on `/api/screener/ws/long-short` with `{type: "position_update", arb_position_id, ...}` messages — the WS connection already exists for live spread.

**E. State machine (arb_position.status)**

```
                                       ┌── user cancels trigger ──→ cancelled
                                       │
pending ──→ opening ──→ open ──┬───────┤
                               │       ├── tp / sl fires ────→ closing ──→ closed
                               │       │
                               │       ├── user "Close at market" ──→ closing ──→ closed
                               │       │
                               │       ├── reconcile: external full close ──→ closed (closed_externally=true)
                               │       │
                               │       └── reconcile: external partial OR one leg failed on portion ──→ partial
                               │
                               └── (Infinite Fill: stays in 'open' indefinitely; new portions keep accumulating)

partial ──┬── user manual close (UI button) ──→ closing ──→ closed
          ├── user manually unwinds the orphan leg ──→ closed
          └── reconcile: matching close on the orphan leg ──→ closed
```

**Invariants**:
- `status='closed'` is terminal.
- `closed_externally=true` ⇒ `status='closed'`.
- TP/SL children fire only when parent is `'open'` or `'partial'`.
- `synced_externally=true` only set on creation via Sync button or reconcile-external-open detection.

**F. P&L finalization**

On `arb_position.status` flip to `'closed'`:

```python
realized = sum(tp.leg_a_realized_pnl_usd + tp.leg_b_realized_pnl_usd
               for tp in arb_pos.trade_positions)
funding  = sum(tp.leg_a_funding_pnl_usd  + tp.leg_b_funding_pnl_usd
               for tp in arb_pos.trade_positions)
arb_pos.realized_pnl_usd = realized + funding
```

Fees are already netted into per-leg `realized_pnl_usd` by each venue's adapter (existing behaviour in `trade_positions`). The P&L tab reads `arb_positions.realized_pnl_usd` directly — no on-the-fly recompute.

**Open positions** show *unrealized* P&L computed live in the frontend from `(current_spread - entry_spread) × notional` plus accumulated funding (read from `trade_positions.leg_*_funding_pnl_usd`, refreshed by `reconcile_service`).

**G. Sync button (manual external opens)**

Sync wraps **two opposite legs into ONE arb entity** with a single TP and a single SL slot (not per leg). The UX intent: «I opened these two positions outside Avalant, please track them as a paired arb so I can attach TP/SL.»

`POST /api/trade/arb-positions/sync`:

1. For every user wallet (`wallet_type IN ('exchange','perpdex')`), call `trade_service.list_user_positions(user)` (with the Task 3 fix applied).
2. Pull spot snapshots for spot-capable wallets via `_list_user_spot_holdings()`.
3. Group by `(symbol_normalized)`. For each group, look for opposing pairs:
   - **long_short**: BUY position on venue A + SELL position on venue B, same symbol, notional within ±12%.
   - **spot_short**: spot holding of asset X + SELL position of X-perp on another venue, notional within ±12% (reuse `list_user_spot_short_pairs` logic — same tolerance).
4. For each pair NOT already wrapped (no existing `arb_position` with these legs in status open/partial), create `arb_position{synced_externally=true, status='open'}`, populate prices/qty from venue state, set `opened_at` to the venue position open timestamp (or `NOW()` fallback). Attach existing `trade_positions` rows for those legs by setting their `arb_position_id`.
5. Return: `{created: [{id, symbol, long_ex, short_ex}, ...], skipped: N, total_scanned: N}`.
6. UI refetches Positions tab → new rows appear with a `[synced]` badge. User can now attach TP/SL via PATCH (single TP and single SL per arb).

**Single TP/SL per arb-entity invariant**: enforced at create-time (HTTP 409 on duplicate, see 7.3 validation). When user replaces TP, the old child trigger is cancelled in the same transaction.

**G2. Auto-pair on internal opens (no Sync needed)**

If a position is opened **through us**, it must always be tracked. Two cases:

1. **Opened via the unified Live Trading panel (arb-orders flow)** — already wrapped: the trigger creates an `arb_position` upfront and links both legs' `trade_positions.arb_position_id` to it on fill. Nothing to do.

2. **Opened via the legacy single-leg endpoint** (`POST /api/trade/open` — manual single-side execution, e.g. user opens just a long on Gate without going through the arb flow):
   - The fill creates a `trade_positions` row with `arb_position_id=NULL` (single leg, no parent).
   - It still appears in the Positions tab immediately (single-leg row, no `[arb]` badge).
   - **Auto-pair detection** runs after every internal fill **and** in the 5-min reconcile pass:
     - For each user `trade_positions` row with `arb_position_id IS NULL` and `status='open'`, look for a counterpart: same `symbol_normalized`, opposite `side`, on a different exchange, notional within ±12%, opened within ±10 min of each other.
     - On match → create `arb_position{synced_externally=false, status='open'}` (NOT externally synced — both legs were ours), set both `trade_positions.arb_position_id` to it, populate entry data from the existing fill records.
     - User sees the pair appear in Positions with the `[arb]` badge — without clicking Sync.
   - Auto-pair NEVER consumes legs that already have `arb_position_id` set, so existing wrapped pairs are safe.

This means: **anything opened through us is 100% tracked**. Sync button is needed only for legs opened on the venue UI directly.

**H. Multi-portion entry, single TP/SL fire**

**H. Multi-portion entry, single TP/SL fire**

When TP/SL fires on a multi-portion position:
- Closes the **currently held qty** (`pos.long_qty` / `pos.short_qty`), not the original `total_qty_token`.
- Cancels remaining `pending`/`scheduled` portions of the parent open trigger (cascade).
- TP/SL itself fires once → `status='fired'`, no re-arm even if `infinite_fill=true` was set on the parent open.

This avoids the "TP fires, then more portions accumulate, then SL fires on what?" confusion.

### 7.7 — Positions tab enhancements

Each position row gains:

- **TP/SL badges**: `[TP 0.3%]` green chip + `[SL 2.5%]` red chip if attached. Click → modal to edit/remove.
- **"Set TP/SL" button** (when no TP/SL attached): opens modal with two inputs.
- **"Sync" button** for `synced_externally=true` rows or when reconcile detects a venue position not yet wrapped in arb_position. Calls `POST /api/trade/arb-positions/sync`.
- **Live spread column**: shows current effective spread vs entry, color-coded (green = profit zone, red = loss zone).
- **"Close at market"** button: kills the position immediately (skip TP/SL).

When `reconcile_service` detects external close (venue says position closed but our `arb_position.status='open'`), set `closed_externally=true` and finalize realized_pnl from venue fills.

### 7.8 — Reduce-only feature flag

Not all venues support `reduceOnly`. Add per-venue capability map:

```python
# backend/services/trade_service.py
VENUE_CAPS = {
    'binance':   {'reduce_only': True,  'post_only': True},
    'bybit':     {'reduce_only': True,  'post_only': True},
    'okx':       {'reduce_only': True,  'post_only': True},
    'gate':      {'reduce_only': True,  'post_only': True},
    'kucoin':    {'reduce_only': True,  'post_only': True},
    'mexc':      {'reduce_only': True,  'post_only': True},
    'bitget':    {'reduce_only': True,  'post_only': True},
    'bingx':     {'reduce_only': True,  'post_only': True},
    'htx':       {'reduce_only': True,  'post_only': False},
    'aster':     {'reduce_only': True,  'post_only': True},
    'kraken':    {'reduce_only': True,  'post_only': True},
    'hyperliquid': {'reduce_only': True, 'post_only': True},
    'paradex':   {'reduce_only': False, 'post_only': True},   # verify
    'ethereal':  {'reduce_only': False, 'post_only': False},
    'lighter':   {'reduce_only': False, 'post_only': False},
    'backpack':  {'reduce_only': True,  'post_only': True},
    'whitebit':  {'reduce_only': False, 'post_only': False},
    'extended':  {'reduce_only': False, 'post_only': False},
}
```

If reduce_only requested on venue without support → adapter sends regular close-side order with qty bound to current `position.qty` (defensive fallback). Logged at INFO so behaviour is observable.

### 7.9 — Plan limits

Add to `plans.features` JSON:

```json
{
  "max_active_triggers": 3,        // Free
  "max_active_triggers": 50,       // Screener-only / Full
  "max_active_triggers": -1        // Unlim
}
```

Enforced at create-time in `POST /api/trade/arb-orders`. 402 with `{"error": "trigger_limit_exceeded", "current": 3, "limit": 3}` on overflow.

### 7.10 — Edge cases & failure modes

| Scenario | Handling |
|---|---|
| Books >5s stale on either leg | Skip tick, retry next 1s. Log every 30s of staleness. |
| `total_qty_token` exceeds 100% of available margin/balance | Reject at create time (pre-flight balance check). |
| User changes leverage / margin_mode / portion_size after create | Allowed via PATCH while `pending`/`scheduled`; locked once `firing`. |
| Venue rejects with `KindUser` (e.g., margin insufficient) | Trigger → `failed`, error_kind='user'. TG alert. If parent open → cascade-cancel TP/SL children. |
| Venue rejects with `KindInternal` (network) | Retry once after 200ms; if still fails → `failed`. |
| User opens manually via /open-arb, later wants TP/SL | "Sync position" button creates `arb_position`; then user opens TP/SL form to attach child triggers. |
| Two triggers fire same tick on overlapping pair | Both fire independently; positions are separate `arb_position` rows. Atomic claim prevents the same trigger firing twice. |
| Reconcile detects external partial close | Reduce `arb_position.long_qty` / `short_qty` accordingly; if either hits 0 → `status='partial'`. Active TP/SL keep firing on remaining qty. |
| TP/SL parent (open) cancelled with portions partially filled | Children also cancelled if they have not yet fired. Filled portions remain as an open position; user closes manually. |
| Infinite-fill trigger keeps firing into an empty book | First portion fails → `status='failed'`, error_kind='exchange'. Service stops re-arming. |
| `activate_at` in past at create time | Treat as immediate (`status='pending'`). |
| User PATCHes a trigger to a value already met by current spread | Same Ok/Cancel modal as POST. Skip with `force=true` to apply and fire next tick. |
| User tries to attach a 2nd TP (or 2nd SL) to an arb_position | HTTP 409 `{"error": "tp_already_exists"}`. UI tells user to PATCH the existing one or DELETE first. |
| User opens a single leg via legacy `/api/trade/open`, then opens the mirror | Auto-pair detection runs on the second fill. New `arb_position` wraps both. No Sync needed. |
| Auto-pair sees a leg already wrapped (arb_position_id is set) | Skip — never re-wrap. |

### 7.11 — P&L semantics

P&L is computed at the **arb-entity level** (paired) when both legs are wrapped under the same `arb_position`, and per-leg when standalone.

**Paired (arb_position)**:

```
arb_position.realized_pnl_usd =
    Σ (trade_positions.leg_a_realized_pnl_usd + leg_b_realized_pnl_usd)   ← price P&L, fees netted
  + Σ (trade_positions.leg_a_funding_pnl_usd  + leg_b_funding_pnl_usd)    ← funding accrued
```

**Standalone leg** (single `trade_positions` with no arb wrap): individual `realized_pnl_usd + funding_pnl_usd`. Shown as a separate row in P&L tab without summing.

**P&L tab columns**:

| Pair | Status | Entry Spread | Exit Spread | Long $ | Short $ | Funding $ | Total $ | Open Duration |
|---|---|---|---|---|---|---|---|---|

For paired rows, `Total = long_realized + short_realized + funding`. For single legs, columns Long $/Short $ collapse into one column.

**Closed-externally data**: when reconcile detects external close (see 7.6.B), it pulls the venue's last close fills to populate `long_exit_price`, `short_exit_price`, per-leg `realized_pnl_usd`. Then the formula above runs once on `status='closed'` flip — same code path as internal closes. The user sees identical P&L data whether they closed via our UI or on the venue directly.

**Open positions** show *unrealized* P&L computed live in the frontend: `(current_spread - entry_spread) × notional + accumulated_funding`. Live spread comes from the existing `_liveBasisPct()` source ([arb.html:3137](frontend/arb.html#L3137)). Funding accumulates via `reconcile_service` writes to `trade_positions.leg_*_funding_pnl_usd`.

### 7.12 — Implementation order (4 sessions)

**Session A (~10h) — Backend foundation**
1. Alembic migration with portion + activate_at + parent_trigger_id (1h)
2. SQLAlchemy models + relationships (`children`, `parent`, `arb_position` backref) (1h)
3. `trigger_order_service.py` — main loop, atomic claim, scheduled→pending promotion (3h)
4. Server-side `_effective_spread()` (VWAP from books.json) (2h)
5. `_accumulate_position()` (VWAP-merge of entry prices across portions) (1h)
6. Service unit tests — single-shot, portioned, infinite-fill, scheduled (2h)

**Session B (~8h) — API + reconciliation**
1. `/api/trade/arb-orders` POST/GET/PATCH/DELETE (3h)
2. `/api/trade/arb-positions` GET/sync/PATCH (2h)
3. Extend `reconcile_service` with arb_positions pass (external close, partial detect, P&L finalize) (2h)
4. Plan-limit enforcement + immediate-execution warning + reduce-only fallback (1h)

**Session C (~11h) — Frontend**
1. Unified Live Trading panel layout — long-short variant (3h)
2. Spot-short variant adaptation (1h)
3. Trigger Spread / Portion Size / TP / SL / Infinite Fill / Start Time wiring (3h)
4. Triggers tab + Edit/Cancel modals + cascade behaviour (2h)
5. Positions tab TP/SL badges + Sync position + Show only spot filter (2h)

After each session: `./scripts/deploy.sh backend` (or `migrations`) and verify on prod with one test pair before proceeding.

### 7.13 — What we explicitly skip in v1

- **Infinity+ mode** (arbion's third tab). Their auto-rebalancing engine is out of v1 scope. Reserve `kind='infinity'` for v1.1 schema-compatibility.
- **Two-sided basis chart** (their +20.43 / -20.77 split chart). Our existing Entry/Exit chart is enough; no work here.
- **Relative TP/SL** (% from entry). Absolute-only in v1 — matches arbion. Add `target_mode='relative'` column later if users ask.
- **Trailing TP/SL** (move stop as profit grows). Add later as `target_mode='trailing'` + delta field.
- **One-cancels-other groups** — TP/SL are independent rows; both can fire if spread oscillates fast (rare; document as known behaviour).
- **Multiple TP / multiple SL per position** (arbion has just one each on the form too). Single TP + single SL per parent in v1.
- **Webhook/IFTTT** external triggers.
- **Non-spread trigger conditions** (volume, funding rate, OI thresholds). v1 is spread-only.
- **Per-portion TP/SL** (close 30% at TP1, 30% at TP2…). Out of v1; portions on the open side, full close on TP/SL side.

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
