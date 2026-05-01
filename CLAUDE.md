# Avalant — Development Guide for Claude

## What is this project

**Avalant** — a web platform for crypto-arbitrage and portfolio management.

In one screen the trader sees funding-rate spreads (long/short between exchanges), spot/perp cash-and-carry, DEX/perp basis, and a unified portfolio view across CEX accounts + EVM/Solana/Tron addresses. Telegram bots handle login, alerts, and subscription notifications.

**Brand**: `avalant_` — Inter 800, 18px, blinking green `_` cursor. Accent `#1AFFAB` (neon green). Logo at `/avalant_favicon.svg`, full at `/avalant-logo.svg`.

**Supported venues**:
- **11 CEX** for screener feeds + portfolio: Binance, OKX, Bybit, Gate, MEXC, KuCoin, Bitget, Backpack, BingX, WhiteBIT, Kraken
- **6 Perp DEX**: Hyperliquid, Aster, Lighter, Ethereal, Paradex, Extended
- **8 Spot exchanges** for spot-short feeds: Binance, Bybit, OKX, Gate, KuCoin, MEXC, Bitget, BingX
- **14 chains**: Tron, Solana, Ethereum, BSC, Polygon, Arbitrum, Optimism, Base, Avalanche, zkSync, Linea, Scroll, Mantle, Blast

**Coverage matrix** (after `feat/dex-parity`):

```
                screener  ob-WS  ob-REST  balance  trade  user-WS  funding-paid
binance/bybit/  ✓         ✓      ✓        ✓        ✓      ✓        ✓
okx/gate/kucoin
mexc/bitget/
bingx/aster/    ✓         ✓      ✓        ✓        ✓      ✓        ✓
hyperliquid/
htx/lighter
kraken          ✓         ✓      ✓        ✓        ✓      ✓        ✓
backpack        ✓         ✓      ✓        ✓        ✓      ✓        ·
whitebit        ✓         ✓      ✓        ✓        ✓      ✓        ·
ethereal        ✓         ·      ·        ✓        ✓      ·        ·
paradex         ✓         ✓      ✓        ✓        R      ·        ·
extended        ✓         ·      ✓        ✓        ·      ·        ·
```

Legend: `✓` shipped · `R` read-only (balance only) · `·` not implemented.

Blocked items:
- **Paradex trading** — `paradex-py 0.5.6` requires `starknet-py <0.29` which is incompatible with Python 3.13. Awaiting upstream.
- **Extended trading** — `x10-python-trading` pins pydantic 2.5.3 / eth-account 0.11 / websockets 12 — would downgrade and break Hyperliquid + Ethereal adapters. Needs separate dep-triage branch.
- **Ethereal orderbook + user-stream** — public WS uses Socket.IO and the SDK's documented stream types (L2Book/Ticker/OrderFill) are all rejected by the live server as "Invalid stream subscription type". REST API has no orderbook endpoint either.

---

## Stack

- **Backend**: FastAPI + uvicorn (2 web replicas + 1 fetcher sidecar), PostgreSQL 16 + Alembic, Redis 7, httpx, websockets
- **Frontend**: vanilla JS + multi-page HTML, no build step, Inter / JetBrains Mono fonts
- **Infra**: Docker Compose, nginx upstream load balancer, Let's Encrypt, PgBouncer (session mode, pool 60)
- **Hot path**: per-exchange funding-WS adapters + REST backstop (pure-thread to avoid event-loop block); orderbook WS multiprocess workers; spot-arb refresh (2s); funding broadcast (300ms compute, 200ms broadcast)
- **Bots**: two-bot mode — `TG_AUTH_BOT_TOKEN` for login + admin alerts + expiry reminders, `TG_BOT_TOKEN` for spread alerts. Either bot can do everything alone (single-bot fallback)

---

## Running

### Local
```bash
source venv/bin/activate
uvicorn app:app --port 8000   # NO --reload (user preference)
# DB: SQLite ./wallet_monitor.db, auto-created via Alembic
```

### Docker / prod
```bash
cp .env.sample .env             # fill secrets
docker compose up -d
```

---

## Deployment workflow

See `DEPLOY.md` for the full picture. There are 6 scopes:

| Command | What rebuilds | User impact |
|---|---|---|
| `./scripts/deploy.sh frontend` | nothing — `git pull`, files bind-mounted | new code on next request, ≤60s browser cache refresh |
| `./scripts/deploy.sh backend` | rolling rebuild app→app2 | zero downtime |
| `./scripts/deploy.sh fetcher` | fetcher sidecar only | 10-20s feed re-warm |
| `./scripts/deploy.sh migrations` | alembic + rolling app/app2 | brief; pair with maintenance for breaking |
| `./scripts/deploy.sh nginx` | nginx reload (or recreate if mount inode flipped) | none |
| `./scripts/deploy.sh all` | everything via legacy rolling-deploy | zero downtime |
| `./scripts/deploy.sh` (no arg) | auto-detects from `git diff` | scope-dependent |

`./frontend` is bind-mounted into all containers as `/app/frontend:ro` — HTML/JS/CSS edits hot-swap without rebuild.

**Rollback**: `git checkout <sha>` then `./scripts/deploy.sh all`.

**Things that don't need a deploy** (runtime via /admin):
- maintenance toggles, plans, promos, popups, billing periods
- hidden symbols, disabled exchanges, trade enable/disable per venue
- expiry-reminder schedule, admin broadcast
- user block/unblock, plan grant

---

## Plan system

**Active paid tiers**: Free (5 wallets, 1 key/venue), Screener-only ($45/mo, 0 wallets, 3 keys/venue), Full ($55/mo, 30 wallets, 3 keys/venue), Enterprise (inactive — kept for future), Unlim (admin-only, -1 = unlimited).

**Billing periods** (4): Scout (1mo, 0% off), Operator (3mo, -10%), Season (6mo, -18%), Desk (12mo, -25%).

`-1` on `portfolio_limit` / `exchange_keys_per_venue` / `portfolio_limit_grace` = unlimited. Surfaced as `null` to the frontend.

**Source of truth**: `users.plan_id` (FK → plans). The legacy `users.plan` string column is a mirror. `plan_service.get_user_plan(db, user)` is the only correct read path.

**Plan upgrade invariant** — `user.plan_id` can only change via:
1. PATCH `/api/admin/users/{id}/plan` (admin-only, `Depends(get_admin_user)`)
2. `payment_service._activate_user` (signature-verified webhook only)
3. Manual SQL on the host

No client-controlled path grants a plan upgrade.

**Admin grant invariant** — `users.is_admin = TRUE` can only happen via:
1. Manual SQL on the host (`UPDATE users SET is_admin=TRUE WHERE …`)

The legacy `INITIAL_ADMIN_USERNAME` and `AVALANT_ALLOW_FIRST_USER_ADMIN` env-var paths were removed (2026-05-02). The TG-widget login also never grants admin. There is no API surface — registering, logging in, linking TG, no combination of those produces an admin.

**Auto-archive on downgrade**: `wallet_quota.enforce_for_user(db, user)` is called from `set_plan` and from `/api/auth/me`. Surplus portfolio wallets archive oldest-first; `purpose='both'` rows downgrade to `'screener'`.

---

## Subscription mgmt

- **Auto-renew flag** (`users.auto_renew`, default True). Cancel sets it False — plan stays active until `plan_expires_at`, but expiry reminders stop firing.
- **Cancel/resume**: `POST /api/auth/me/subscription/cancel` and `/resume`. Profile page shows the right state (Active / Cancelled) + Renew + Cancel/Resume buttons.
- **Expiry-reminder daemon**: `backend/services/expiry_notifier_service.py` runs every 30 min on the fetcher container. Scans users with `auto_renew=True` + `plan_expires_at` within `expiry_notice_days` (default 3, range 0–60) + `tg_chat_id` set. Sends via auth bot. Per-user throttle via `users.expiry_notice_last_sent_at` so daemon restarts don't double-fire. Schedule is admin-tunable from `/admin → Communications`.
- **Renew flow**: button on /profile → `/pricing?renew=<plan-slug>`.

---

## Maintenance modes

Three independent scopes, each with optional ETA + countdown + IANA timezone (default `Europe/Warsaw`).

| Scope | Blocks | Stays open |
|---|---|---|
| `site` | every page | `/api/health`, `/api/maintenance/status`, admin API |
| `screener` | `/screener`, `/arb`, `/watchlist`, `/api/screener/*` | portfolio + admin + pricing |
| `portfolio` | `/app`, `/archive`, `/profile`, `/avashare`, wallet/alert/trade APIs | screener + pricing + checkout |

**Lockout pages auto-reload** when the scope flips back off — they poll `/api/maintenance/status` every 15s.

**Set ETA in one round-trip** (admin):
```bash
curl -X POST https://avalant.xyz/api/admin/maintenance \
  -H "Authorization: Bearer $JWT" -H 'Content-Type: application/json' \
  -d '{"scope":"screener","enabled":true,"duration_minutes":45}'
```

Display tz settable via `/admin → Maintenance`.

---

## Security

### Authentication
- JWT bearer (HS256), `jti` revocation in Redis (`backend/services/token_blacklist.py`)
- Bcrypt passwords (passlib 1.7.4 + bcrypt>=4,<5)
- `scope` claim on TOTP-challenge tokens — rejected by `get_current_user` so a leaked challenge can't masquerade as a session
- HttpOnly + Secure session cookie (override `AVALANT_COOKIE_SECURE=0` for localhost dev only)
- 30-day rolling cookie + JWT lifetime

### Admin
- **Promotion is SQL-only**: there is exactly one path — direct UPDATE on the host. `INITIAL_ADMIN_USERNAME` and `AVALANT_ALLOW_FIRST_USER_ADMIN` are no longer honoured. TG-widget login never grants admin.
- **TOTP 2FA** (admin-only): `pyotp` + Fernet-encrypted secret at rest. `users.totp_verified_at` is the armed flag. Login flow gates admin sessions on a second factor when `totp_verified_at` is set. Failed TOTP triggers `admin_alert_service.alert_admin_security` to admin TG.
- **Honeypot autoban** (`backend/services/honeypot_service.py`): a logged-in non-admin who hits `/api/admin/*`, `/admin`, or `/admin-user` is auto-blocked, audit-logged (`security.admin_probe_block`), and admins get a TG ping. Anonymous probes get plain 401 — too noisy to ban automatically.

### Rate limit
- Redis-backed sliding-window via `INCR + EXPIRE` (`backend/services/rate_limit.py`). Falls back to in-memory on Redis blip with 10s backoff.
- Buckets: `payments_checkout` (5/min), `promo_validate` (10/min), `wallets_create` (30/h), `admin_write` (60/min).
- IP key uses `X-Forwarded-For`; rate-limit endpoint in `auth.py` applies its own per-IP attempt counter on `/auth/*` (10/60s).

### Encryption
- Fernet on `wallets.credentials` (PBKDF2-SHA256 from `ENCRYPTION_KEY`, 260k iterations, hardcoded salt `b"wallet-monitor-creds-v1"`)
- Same on `users.totp_secret_enc`
- Cached Fernet instance — derive once per process
- **Rotation**: `scripts/rotate_encryption_key.py` re-encrypts every wallet credential + every TOTP secret with a new key. Reads OLD/NEW from env; partial-rotation safe (each row tries OLD then NEW).

### Headers
- `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `X-XSS-Protection: 1; mode=block`
- `Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy: geolocation=(), camera=(), microphone=()`
- **CSP**: `default-src 'self'; script-src 'self' 'unsafe-inline' https://unpkg.com; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; ...; frame-ancestors 'none'; upgrade-insecure-requests`. Skipped on `/api/*`.
- HSTS 1-year via nginx
- OpenAPI hidden (`docs_url=None, redoc_url=None, openapi_url=None`)

### WebSocket auth
- First-frame `{"auth": "<JWT>"}` after `accept()`. URL no longer carries the token (used to leak into nginx access logs).
- 5s timeout → `close(4401)`. Applied to `/ws/funding`, `/ws/long-short`, `/ws/arb` (legacy alias), `/ws/book`.

### CryptoCloud webhook
- Strict signature validation (`payment_service.verify_webhook_signature`). If `CRYPTOCLOUD_WEBHOOK_SECRET` is unset, the route returns 503 + critical-level log (refuses, never fail-open).
- Bad signature → 401 + WARNING with source IP + payload key list.
- Idempotent: `SELECT … FOR UPDATE` on payments row + early-return if already paid.

### CSRF
Intentionally NOT added — every `/api/*` uses `Authorization: Bearer` + JSON body, never cookie form-post. The HttpOnly session cookie only gates HTML rendering in `serve_page`. CSRF tokens would add ceremony without closing real surface.

---

## Telegram bots

**Two-bot deployment**:
- `TG_AUTH_BOT_TOKEN` / `TG_AUTH_BOT_USERNAME` — login flow, admin alerts, expiry reminders, broadcasts
- `TG_BOT_TOKEN` / `TG_BOT_USERNAME` — arb-spread alerts to users (`alert_service`)

Either token alone runs everything (single-bot fallback). Auth-bot helpers `_auth_bot_token()` / `_auth_bot_username()` in `tg_auth_service.py` always prefer the auth bot when configured.

### Leader election (`backend/services/tg_bot_service.py`)
Two web replicas + fetcher all import `start_tg_bot()`, but only ONE process polls each bot at a time. Redis SETNX with TTL 30s + compare-and-set Lua renew every 10s (`tg_bot_lock:<sha256(token)[:16]>`). If the leader crashes, the next replica picks up within 30s.

Without `REDIS_URL`, falls back to single-replica polling — same race as before but no cross-replica coordination.

### Login flow
1. User opens `/login` → clicks "Sign in with Telegram"
2. Frontend issues `POST /api/auth/tg-bot-login` → gets `deep_link: t.me/<auth_bot>?start=auth-<token>`
3. User taps Start in the bot
4. Bot's `_handle_update` calls `consume_login_token` → writes JWT to `/tmp/avalant_cache/login_<hash>.json` AND replies with an inline-keyboard button "🔓 Open Avalant" → `https://avalant.xyz/tg-done?t=<token>`
5. User taps button → `tg-done.html` calls `GET /api/auth/tg-bot-login?token=X` → `Auth.setSession`, redirect to `/app`

The button is the load-bearing fix — mobile browsers freeze the originating /login tab when TG opens, killing the polling. Button-driven redirect bypasses that.

### tg_username refresh
TG usernames can change but `tg_id` + `tg_chat_id` are stable. Every `/start` resyncs `users.tg_username` to the current handle so the legacy username-fallback path keeps working.

### Admin broadcast
`POST /api/admin/broadcast {text, target='all'|'user', target_user_id?, parse_mode='HTML'}`. Sends via auth-bot, concurrency cap 20, audit-logged. Cap 4000 chars. Tab in `/admin → Communications → Broadcast`.

### Delisted-symbol filter (Binance specifically)
Binance keeps delisted symbols in `/api/v3/ticker/24hr` (`status=BREAK`) and `/fapi/v1/premiumIndex` (`status=SETTLING`) for days. We cross-check against `/exchangeInfo`'s `status=='TRADING'` set, cached 10 min. Applies to:
- `spot_arbitrage_service._fetch_binance_spot` (filter via `isSpotTradingAllowed`)
- `arbitrage_service._fetch_binance` (filter via `contractType=PERPETUAL`)
- `funding_ws.adapters.BinanceFundingWS` — both the WS push and REST backstop. Aster inherits via subclass.

NTRN is the canary — if it appears in the feed, the filter is broken.

---

## Promo codes

Each promo can grant a price discount AND/OR bonus subscription days, scoped per-user or per-target.

**Fields** (`promo_codes`):
- `discount_pct` (0–100) — % off the cart total
- `bonus_days` (0–3650) — days appended to `payment.activated_until`
- `max_uses` (null = ∞) — total usage cap
- `per_user_max_uses` (null = ∞, 1 = once per user) — caps PromoCodeUsage rows for the same user
- `target_user_id` (FK → users, null = anyone) — code only redeemable by this user
- `applies_to_plan_ids` (JSON list, null = all paid plans)
- `expires_at`, `is_active`

**Invariant**: at least one of `discount_pct > 0` or `bonus_days > 0` must hold. Pure trial extensions are valid (`EARLY7` = 0% + 7 days).

**Validation flow**: `validate_for_plan(db, code, plan_id, user_id=...)` is the canonical gate. Called from `/api/promo/validate` (frontend pre-check) AND from `payment_service.create_invoice` (server-side, before invoice creation). Both pass `user_id` so per-user + target enforcement work.

**Activation**: `_activate_user` reads the promo's `bonus_days` and adds to `activated_until` after the regular billing-period window. Logged at INFO.

---

## Popups

Admin-managed targeting popups, four audience modes:

| `target_type` | Audience |
|---|---|
| `everyone` | both authenticated and anonymous visitors |
| `authenticated` | every logged-in user (legacy `all` migrates here) |
| `anonymous` | logged-out visitors only |
| `user` | specific `target_user_id` |

`/api/popups/pending` is auth-optional — anonymous visitors get `everyone + anonymous`, logged-in get `everyone + authenticated + matching user`. Anonymous dismissals stored in `localStorage["avalant_popup_anon_dismissed"]` (no DB row possible without user_id).

Popup styling: backdrop with green radial glow, ID-pill in header, footer with "Maybe later" + CTA. `popup.js` is included on every auth-protected page + `/pricing`.

---

## Database

Production: PostgreSQL 16 via PgBouncer (session mode, pool 60). Local: SQLite. Migrations run automatically on `app` startup via `alembic upgrade head`.

### Tables
- **users** — id, username, email, hashed_password, is_admin, is_blocked, plan (legacy), plan_id (FK), plan_expires_at, request_count, last_active_at, created_at, email_verified_at, tg_username, tg_chat_id, tg_id, totp_secret_enc, totp_verified_at, **auto_renew**, **expiry_notice_last_sent_at**
- **wallets** — id, name, wallet_type, type_value, credentials (JSON, Fernet), is_archived, can_trade (legacy), purpose ('portfolio' | 'screener' | 'both'), is_main, user_id
- **tags** — id, name, color, user_id (NULL = system tag)
- **wallet_tags** — M2M, CASCADE
- **wallet_addresses** — named addresses for the address book
- **balance_snapshots** — one per wallet, totals JSON, stable_total Float
- **provider_error_logs** — error_type bucket (rate_limit / auth / network / unknown)
- **balance_history** — per-user aggregate USD over time
- **arb_alerts** — user-defined spread thresholds, 1h cooldown via `last_triggered_at`
- **plans** — slug, name, price_usd_monthly/annual, portfolio_limit, portfolio_limit_grace, exchange_keys_per_venue, trade_delay_ms, has_portfolio, is_subscription, is_admin_only, features JSON, is_free, is_active, sort_order
- **billing_periods** — slug, label, months, discount_pct, sort_order, is_active
- **promo_codes** — code, discount_pct, **bonus_days**, max_uses, used_count, **per_user_max_uses**, **target_user_id**, applies_to_plan_ids, is_active, expires_at
- **promo_code_usages** — ledger: promo_code_id × user_id × payment_id × discount_pct (used by per-user cap check + revenue stats)
- **payments** — CryptoCloud invoice lifecycle (pending → paid / failed / expired)
- **popups** — title, body, button_text/url, **target_type** (`everyone / authenticated / anonymous / user`), target_user_id, frequency_type (`once` / `every_n_min`), frequency_minutes, is_active
- **popup_dismissals** — per-user dismissal log (anon dismissals are localStorage)
- **app_settings** — key/value JSON for runtime knobs (maintenance, hidden symbols, etc.)
- **audit_log** — append-only ledger; admin / billing / security actions
- **tg_link_tokens** — sha256 of single-use deep-link tokens (15min TTL)
- **password_reset_tokens**, **email_verify_tokens** — sha256, 1h TTL

Recent migrations:
- `a4b5c6d7e8f9` — users.auto_renew + expiry_notice_last_sent_at
- `b5c6d7e8f9a0` — promo per_user_max_uses + target_user_id
- `z3a4b5c6d7e8` — promo bonus_days
- `y2z3a4b5c6d7` — popup target_type expansion (`all` → `authenticated`)
- `x1y2z3a4b5c6` — admin TOTP 2FA columns
- `w0x1y2z3a4b5` — Unlim plan + delete test users (cascade)

---

## Architecture: hot path

### Funding feed (≤2.6s freshness across 11 venues)
Each venue runs **two concurrent loops**:
1. **WS task** on the asyncio event loop (`_run`) — primary sub-second updates
2. **REST backstop** in a **pure daemon thread** (`rest_refresh_sync`) — every 2s, merges into `self._rows` directly. Pure thread because `loop.run_in_executor` was blocking 5-6s under the full fetcher load.

Dict key-assignment is GIL-atomic, so cross-thread writes work without locks.

REST backstops covering known WS gaps:
- Bybit (volume on partial updates)
- OKX (price/volume — WS only has rate)
- Gate, Bitget — full ticker + rate
- KuCoin (volume + rate — WS lies)
- MEXC (rate — WS doesn't supply it)
- BingX (caps WS at ~100 symbols, REST fills all 600)

Per-symbol max age across 10 exchanges: 0.04–2.62s.

### Spot-arb (separate from futures)
- Dedicated httpx client. **Never share the pool with arb `_http`** — the 8-spot gather starves under load.
- `_spot_refresh_loop` runs every 2s on fetcher, file-locks `/tmp/avalant_spot_refresh.lock` (flock auto-releases on process death; no orphan-recovery needed).
- Web role reads `spot_arbitrage.json` with 120s max-age. Never computes.
- Ticker-collision filter: `|basis_pct| > 5%` rows dropped (e.g. MEXC "META" ≠ KuCoin "META").

### Arb compute (300ms cycle)
- `AVALANT_REFRESH_INTERVAL=0.3` (300ms compute), `AVALANT_BROADCAST_INTERVAL=0.2` (WS push every 200ms)
- Two-tier cache: `_cache` (6s, price/rate), `_ivl_cache` (5min, intervals)
- MEXC/Bitget interval fetch is slow (~40s); they're in `_SLOW_IVL` and never block user requests — fall back to 4h default while bg refresh runs

### Orderbook
- Multiprocess workers (`AVALANT_FETCHER_MODE=multiprocess`) — one process per exchange, dumps `books.<ex>.json`
- Web reads `books.json` (merged), polls per pair from `_arb_http` (HTTP/1.1 keepalive, 200 conns, 60 keepalive)
- 150ms per-side refresh on `/arb` page

### WS broadcast
- `/ws/funding` (full snapshot, ~300KB gzip)
- `/ws/long-short` (delta-encoded, ~3-10KB per tick) — canonical
- `/ws/arb` (legacy alias)
- `/ws/book` (orderbook diffs, server-side filtering by subscribed pairs)

All four use first-frame auth (no `?token=` in URL).

---

## API

| Method | Path | Auth | Notes |
|---|---|---|---|
| `GET /api/health` | — | — | uptime probe |
| `GET /api/maintenance/status` | — | — | public; lockout pages poll this every 15s |
| `GET /api/metrics` | — | — | Prometheus text format |
| `GET /api/health/feeds`, `/api/health/fetcher` | — | — | per-feed staleness JSON |
| `POST /api/auth/register/login/logout` | — | bcrypt + JWT |
| `GET /api/auth/me` | Bearer | enriches with `auto_renew`, `totp_enabled`, plan info; runs `wallet_quota.enforce_for_user` |
| `POST /api/auth/me/subscription/cancel` | Bearer | sets `auto_renew=False` |
| `POST /api/auth/me/subscription/resume` | Bearer | sets `auto_renew=True` |
| `POST /api/auth/login/totp` | — | second-factor for admins |
| `POST /api/auth/me/2fa/setup`, `/verify`, `/disable` | Bearer | admin-only TOTP CRUD |
| `POST /api/auth/tg-login`, `/tg-bot-login` | — | TG widget + bot login |
| `GET /api/auth/tg-bot-login?token=` | — | poll endpoint for the bot login |
| `POST /api/auth/me/tg-link-token` | Bearer | issue deep-link |
| `GET /api/admin/*` | Admin | 36 endpoints, all gated by `Depends(get_admin_user)` (which trips the honeypot on non-admins) |
| `POST /api/admin/maintenance` | Admin | `{scope, enabled, duration_minutes, tz?}` |
| `POST /api/admin/broadcast` | Admin | `{text, target, target_user_id?, parse_mode}` |
| `GET /api/screener/funding/long-short/spot-short/dex-short/all-arbitrage` | Bearer | canonical feeds |
| `GET /api/screener/orderbook/arb-price-history/arb-history/all-exchanges-funding/open-interest` | Bearer | per-pair detail |
| `GET /api/screener/arbitrage`, `/spot-arbitrage`, `/dex-arbitrage` | Bearer | legacy aliases |
| `WS /api/screener/ws/funding/long-short/arb/book` | first-frame `{auth}` | live feeds |
| `GET/POST /api/wallets`, `/api/wallets/options`, `/api/wallets/all-addresses`, `/api/wallets/{id}/addresses` | Bearer | wallet CRUD + address book |
| `POST /api/portfolio/balance/transactions/transactions/bulk`, `GET /api/portfolio/history` | Bearer | balance fetcher |
| `GET/POST/PATCH/DELETE /api/alerts` | Bearer | spread alerts |
| `POST /api/promo/validate` | Bearer | dry-run promo against plan + period |
| `POST /api/payments/checkout` | Bearer | CryptoCloud invoice |
| `POST /api/payments/cryptocloud/webhook` | sig-verified | strict — refuses if secret unset |
| `GET /api/popups/pending`, `POST /api/popups/{id}/dismiss` | optional Bearer | popup loader (auth + anon) |
| `GET /api/plans`, `/api/billing-periods` | — | public catalogue |
| `GET/POST/PATCH/DELETE /api/trade/*` | Bearer | open/close positions, list orders |

---

## Frontend pages

| Page | Auth | Notes |
|---|---|---|
| `/` index | — | legacy landing |
| `/landing` | — | new full-bleed landing (pre-launch lock disables product/auth links) |
| `/login`, `/register` | — | JWT issuance, HttpOnly cookie set |
| `/tg-done` | — | bridge page for the TG bot's "Open Avalant" button |
| `/pricing`, `/checkout` | — | Scout/Operator/Season/Desk plan cards |
| `/app` | auth | portfolio main — wallets, balances, transactions |
| `/archive` | auth | archived wallets with PORTFOLIO/SCREENER/BOTH purpose badge |
| `/profile` | auth | plan card, balance history chart, **Renew/Cancel renewal** controls, TG-link, AvaShare button |
| `/avashare` | auth | UI-only referral page (localStorage, backend pending) |
| `/screener` | auth | 5 modes: All / Long-Short / Spot-Short / DEX-Short / Funding / Alpha. Mobile cards rendered for every mode |
| `/arb` | auth | per-pair terminal (charts + dual orderbooks + alerts modal) |
| `/watchlist` | auth | WS feed with delta + alpha overlay |
| `/admin`, `/admin-user` | admin | KPI, users, plans, promos, popups, billing periods, **Communications** (broadcast + expiry schedule), **Maintenance** (3-scope ETA) |
| `/maintenance` | — | full-site lockout page (also rendered inline by middleware) |
| `/404` | — | terminal-themed |

### Shared modules
- `auth.js` — `Auth.{getToken, setSession, requireAuth, requireAdmin, isAdmin, logout, apiFetch}`. `apiFetch` prepends `/api/`.
- `toast.js` — `toast(msg)`, `toast(msg, 'success'|'error'|'warn'|'info')`, `toast(msg, type, sub)`, `toast({title, type, sub, duration})`
- `theme.js` — light/dark scaffold (toggle button auto-injection disabled)
- `navbar.js` + `navbar.css` — `<app-navbar page="...">` custom element
- `popup.js` — promotional popup loader, anon-aware
- `confirm.js` — universal confirm modal (replaces native confirm)
- `expiry-banner.js` — top banner when plan_expires_at is near
- `formatters.js` — number/price/volume helpers
- `exchanges.js` — single source of truth for `EX.labels`, `EX.colors`, `EX.dot()`, `EX.chip()`

### Design language
```
--bg:       #0E0E11   --green:   #1AFFAB
--surface:  #131217   --red:     #F87171
--surface2: #17171C   --yellow:  #E5C07B
--surface3: #202028   --teal:    #06B6D4
--border:   #22222A   --purple:  #925BD6
--text:     #E6E8E3
--text2:    #9B9FAB
--text3:    #676B7E
```

Light theme exists in `body.light` with `#006B3C` green + `#8B0000` red (deeper for white-bg readability). Toggle button intentionally disabled.

Fonts: Inter (UI), JetBrains Mono (numbers/prices/addresses).

---

## Trade adapters

Currently shipping `place_order` + `close_position` + `set_leverage` for: **binance, bybit, okx, gate, kucoin, mexc, bitget, bingx, hyperliquid, aster, ethereal, backpack, lighter, kraken, htx, whitebit**.

**Read-only** (in `_READONLY` set): `paradex` (Stark signing blocked by Python 3.13 + paradex-py SDK constraints).

**Spot/short pair detection**: `list_user_spot_short_pairs()` cross-references open SHORT futures positions with non-stable spot holdings from `BalanceSnapshot.totals`. Auto-pairs when notional matches within ±5% AND (if the spot snapshot is fresh) the spot was last refreshed within ±10 min of the short open. Manual paired/unpaired decisions persist in the same `TradePairDecision` table with `leg_a_key` prefix `spot|`. Endpoint: `GET /api/trade/spot-short-pairs`.

**Funding-paid tracking**: `funding_pnl_usd` is populated on live positions for binance, bybit, okx, aster, gate, kucoin, mexc, bitget, bingx, hyperliquid, kraken, lighter, htx. `reconcile_service` mirrors the field into `leg_a_funding_pnl_usd` so closed-pair P&L correctly nets out funding cost.

**Trade delay** (`plan.trade_delay_ms`) is enforced in BOTH `place_open_order` AND `close_position` in `trade_service.py`. Free plan: 500ms throttle.

**Trade gate per exchange**: `admin_settings.get_trade_disabled_exchanges()` blocks new positions on selected venues without disabling screener/funding/portfolio for them.

**Order types**: market only. Limit / stop / TP listed in `TODO.md`.

---

## Admin services

### Audit log (`audit_log.py`)
Append-only `audit_log` table. Two entry points:
- `record(db, request, actor, action, target_type, target_id, delta)` — primary, with Request object for IP/UA
- `record_low_level(db, actor_user_id, actor_ip, action, ...)` — for callers without a Request (background services, middleware, honeypot)

Every destructive admin endpoint records here. Admin reads via `GET /api/admin/audit-log` filtered by action / actor / target.

Tags include: `users.block`, `users.plan`, `plans.create/update/delete`, `promos.create/update/delete`, `popups.create/update/delete`, `billing_period.create/update/delete`, `admin.broadcast`, `security.admin_probe_block`.

### Admin alerts (`admin_alert_service.py`)
`notify_admins(text, parse_mode='HTML')` — fire-and-forget broadcast to every `is_admin=True` user with `tg_chat_id` set, via auth bot. 60s in-process dedup.

Convenience wrappers: `alert_payment(user, plan_slug, amount)`, `alert_admin_security(user, event, ip)`, `alert_user_blocked(user, reason)`.

Wired up from:
- `payment_service._activate_user` → payment-received notification
- `auth.login_totp` (failed code path) → admin security event
- `admin.toggle_block` (when blocking) → user-blocked notification
- `honeypot_service.trip` → admin probe blocked

### Settings (`admin_settings.py`)
Read-through cache (15s TTL) over the `app_settings` table. Keys:
- `hidden_symbols`, `disabled_exchanges`, `disabled_wallet_exchanges`, `disabled_chains`, `disabled_perpdexes`
- `maintenance_mode`, `screener_disabled`, `portfolio_disabled`
- `trade_disabled_exchanges`
- `arb_min_volume_usd`, `arb_exclude_exchanges`
- `expiry_notice_days`, `expiry_notice_interval_hours`
- `maintenance_ends_at`, `screener_disabled_ends_at`, `portfolio_disabled_ends_at`, `maintenance_tz`

---

## Provider system

Each provider is a class with metadata-driving attributes:

```python
class BinanceProvider(BaseWalletProvider):
    name = "BinanceProvider"
    label = "Binance"
    enabled = True              # False → hidden from UI
    needs_passphrase = False
    needs_api_key = False
    soon = False                # for perpdex — shows "soon" badge
```

`WALLET_OPTIONS` is auto-generated from these. Disable a provider by setting `enabled = False` on the class (or `"enabled": False` in `CHAIN_META` for chains).

---

## Logging

Centralized `setup_logging(role)` in `backend/logging_config.py`:
- Console + rotating files (10MB × 5 per channel) under `<LOG_DIR>/<role>/`
- Separate `errors.log` (WARNING+)
- `AVALANT_LOG_FORMAT=json` switches to structured JSON output (`JsonFormatter`)
- Uncaught-exception hooks: `sys.excepthook`, `threading.excepthook`, asyncio loop exception handler

Roles: `web` (uvicorn), `fetcher` (data-plane sidecar), `monolith` (local dev).

---

## Environment variables

```env
# Required
DATABASE_URL=postgresql://wallet:PASS@pgbouncer:5432/avalant
SECRET_KEY=<long-random>
ENCRYPTION_KEY=<long-random>
POSTGRES_PASSWORD=<for-docker>

# Optional but production-critical
REDIS_URL=redis://redis:6379/0
PUBLIC_BASE_URL=https://avalant.xyz
ALLOWED_ORIGINS=https://avalant.xyz
ACCESS_TOKEN_EXPIRE_DAYS=30
LOG_LEVEL=INFO
AVALANT_LOG_FORMAT=text|json
AVALANT_COOKIE_SECURE=1

# Telegram bots
TG_BOT_TOKEN=<alerts-bot-token>
TG_BOT_USERNAME=avalant_bot
TG_AUTH_BOT_TOKEN=<login-bot-token>      # optional; falls back to TG_BOT_TOKEN
TG_AUTH_BOT_USERNAME=avalant_login_bot

# Payments (CryptoCloud)
CRYPTOCLOUD_API_KEY=...
CRYPTOCLOUD_SHOP_ID=...
CRYPTOCLOUD_WEBHOOK_SECRET=...           # WEBHOOK REFUSES if unset (503)
CRYPTOCLOUD_SUCCESS_URL=https://avalant.xyz/checkout?status=success
CRYPTOCLOUD_FAIL_URL=https://avalant.xyz/checkout?status=fail

# Market data
ANKR_KEY=<recommended for EVM tokens>
TRON_KEY=<optional pro tier>
SOLANA_RPC=...
CMC_API_KEY=<for top-100 USD prices>

# Per-EVM RPC (optional if ANKR_KEY set)
ETHEREUM_RPC=, BSC_RPC=, POLYGON_RPC=, ARBITRUM_RPC=, OPTIMISM_RPC=, BASE_RPC=,
AVALANCHE_RPC=, FANTOM_RPC=, ZKSYNC_RPC=, LINEA_RPC=, SCROLL_RPC=, MANTLE_RPC=,
BLAST_RPC=

# Tuning
AVALANT_REFRESH_INTERVAL=0.3
AVALANT_BROADCAST_INTERVAL=0.2
AVALANT_ARB_CACHE_TTL=0.4
AVALANT_FETCHER_MODE=multiprocess
AVALANT_WORKER_EXCHANGES=binance,bybit,okx,...

# Dev only — NEVER set in production
AVALANT_AUTH_DEV_EXPOSE_TOKEN=1          # leak password-reset / verify-email tokens in JSON
```

---

## Important gotchas (curated)

1. **`wallet.provider` is a class**, not an instance. Use `wallet.provider()` to instantiate.
2. **Passphrase exchanges**: OKX, KuCoin, Bitget. Indicated by `needs_passphrase = True`.
3. **`credentials` in DB** is Fernet-encrypted. Cache: derive once per process.
4. **`return_exceptions=True`** in `asyncio.gather` — one provider failing doesn't crash others.
5. **First user is NOT admin** — admin is SQL-only on the host. `INITIAL_ADMIN_USERNAME` and `AVALANT_ALLOW_FIRST_USER_ADMIN` env vars are no longer honoured.
6. **SQLite quirk**: use `sa.true()` / `sa.false()` in migrations, not `'true'` strings — SQLite stores literal text and Python treats it as truthy.
7. **bcrypt<5**: passlib 1.7.4 is incompatible with bcrypt 5.x. Pin `bcrypt>=4,<5`.
8. **`postgres://` → `postgresql://`** normalization in `db/base.py` and `alembic/env.py`.
9. **Wallet limit on backend** via `plans.wallet_limit` / `wallet_quota.enforce_for_user`. 402 on exceed.
10. **KuCoin** requires `pageSize >= 10` for deposits/withdrawals.
11. **Bitget** v2 deposit/withdrawal endpoints 404 — use bills.
12. **`/api/wallets/all-addresses` BEFORE `/{wallet_id}`** in route declaration order.
13. **MutableHeaders has no `.pop()`** — use `del headers["key"]` after `if "key" in headers`.
14. **`form.elements['name'].value`**, never `form.name.value` (returns "").
15. **Tag dropdown** rendered as a portal in `document.body` to escape `overflow-y: auto` clipping.
16. **`request_count`** incremented only on `/balance` and `/transactions` endpoints.
17. **Lighter HTTP 400 = unregistered address** (normal), not an error.
18. **`/promo/validate` must pass `user_id`** for per-user / target_user_id enforcement.
19. **Plan-id, not plan-string** is the source of truth. The legacy `users.plan` is a mirror — admin `set_plan` writes both.
20. **Webhook signature must validate** — if `CRYPTOCLOUD_WEBHOOK_SECRET` is empty, the route returns 503. Never fail-open.
21. **fcntl.flock auto-releases** on process death — spot-refresh lock-orphan recovery is automatic.
22. **WS auth is first-frame**, never URL `?token=`.
23. **TG bot polling uses Redis lock** — only one replica polls at a time. Without Redis, falls back to no-coordination polling.
24. **Honeypot trips on `/api/admin/*`, `/admin`, `/admin-user`** for non-admins — the user is auto-blocked.
25. **Frontend bind-mounted as `/app/frontend:ro`** — host edits propagate without container restart.
26. **Cache-Control on JS/CSS is 60s** (set by `serve_page`). Static images get 86400s.
27. **CSP `unsafe-inline` is required** for now — every HTML ships inline `<script>`/`<style>`. Drop only when we move to a hash-based bundle.
28. **`/api/maintenance/status` is public** so lockout pages can poll-and-reload.
29. **Binance delisted symbols** (e.g. NTRN) keep returning in `/ticker/24hr` and `/premiumIndex` — filter via `/exchangeInfo` `status='TRADING'`. Cached 10 min.
30. **WS funding REST backstop is a pure thread**, not `asyncio.to_thread` — `loop.run_in_executor` was blocking 5-6s under load.
31. **Spot httpx pool is dedicated** — never share with `arbitrage_service._http`.
32. **`auto_renew=False` ≠ plan ended** — plan is active until `plan_expires_at`, just no expiry pings.
33. **Promo bonus_days** add to `activated_until` AFTER the regular billing-period window.
34. **Maintenance ETAs auto-clear when in the past** — `_ends_at()` returns None for stale ISO strings so the lockout page doesn't show "ended 2h ago".
35. **`/avashare` and `/api/popups`** are blocked by portfolio maintenance scope. `/pricing` and `/checkout` stay open intentionally so users can renew.
36. **Encryption key rotation**: `python scripts/rotate_encryption_key.py` with `AVALANT_OLD_ENCRYPTION_KEY` + `AVALANT_NEW_ENCRYPTION_KEY` env. Idempotent — re-run after a partial failure tries OLD then NEW per row.
37. **register strips secrets**: `AVALANT_AUTH_DEV_EXPOSE_TOKEN=1` is the only way to get raw password-reset / email-verify tokens back from the API. Default never exposes.
38. **Admin promotion path**: SQL on the host. Only path. Legacy env vars removed.
39. **2FA TOTP gates admin login only**. Regular users have no 2FA option yet.
40. **Compose env-block must list each var** — a host `.env` entry isn't auto-forwarded; `x-app-env: &app-env` in `docker-compose.yml` enumerates every `${VAR:-default}` it passes into containers.

---

## Common workflows for Claude

### "Add a new exchange"
1. Create provider in `backend/providers/exchanges/<name>_provider.py` (inherit `BaseWalletProvider`)
2. Set class attrs: `name`, `label`, `enabled = True`, `needs_passphrase`
3. Implement `fetch_balance(wallet) → BalanceResult`
4. Register in `EXCHANGE_PROVIDERS` dict (`backend/providers/exchanges/__init__.py`)
5. Add to `ExchangeType` enum
6. Add `_<name>_txs(creds)` in `transaction_service.py` + wire into dispatcher
7. Optional: add screener fetcher to `arbitrage_service.FETCHERS`
8. Optional: add trade adapter to `trade_adapters/` and remove from `_READONLY`

### "Make a runtime change without deploy"
- Plans, promos, popups, billing periods → `/admin → Monetisation`
- Hidden symbols, disabled exchanges, trade-disabled venues → `/admin → Screener`
- Maintenance scope + ETA → `/admin → Maintenance`
- Expiry-reminder schedule, broadcast → `/admin → Communications`
- User block, plan grant → `/admin → Users`

### "Frontend-only change"
1. Edit `frontend/*.html|js|css`, commit, push
2. On prod: `./scripts/deploy.sh frontend` (just `git pull` — files are bind-mounted)
3. Done. Cached browsers refresh JS within 60s

### "Backend-only change"
1. Commit, push
2. On prod: `./scripts/deploy.sh backend` — rolling rebuild app→app2

### "Migration"
1. `alembic revision -m "..."` locally, write upgrade/downgrade
2. Test on local SQLite + Postgres
3. `./scripts/deploy.sh migrations` on prod (pair with full-site maintenance for breaking schemas)

### "Add an env var"
1. Add to `backend/settings.py` (Pydantic BaseSettings)
2. Add to `docker-compose.yml` `x-app-env:` block (`VAR_NAME: ${VAR_NAME:-default}`)
3. Add to `.env.sample`
4. On prod: append to `.env`, then `docker compose up -d app app2 fetcher` (no rebuild needed — env recreate)

---

## Workflow conventions

### Git
- Conventional commits: `feat:`, `fix:`, `style:`, `perf:`, `refactor:`, `docs:`, `chore:`
- Body: bulleted notable changes
- Trailer: ALWAYS `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`
- Use HEREDOC for multi-line commit messages
- "коммит и пуш" = `git add <specific files> && git commit -m "..." && git push`. Never `git add -A`
- Never `--amend`, `--force-push`, or skip hooks unless explicitly asked
- Stage explicitly — list files on `git add`

### Code style
- No comments unless non-obvious WHY
- No premature abstractions — three similar lines beats a helper
- No defensive code inside trusted boundaries
- No backwards-compat shims for in-development features
- Concise responses — final answers ≤100 words unless explaining architecture

### When user says X, do Y
| User says | Action |
|---|---|
| "коммит и пуш" | Stage specific files, commit with conventional message, push |
| "перезапусти" | kill uvicorn + restart |
| "убери" | Remove the last added thing + its CSS + JS + state |
| "скинь что видно" / "выведи" | Dump raw data in fenced code block |
| "сделай попривлекательнее" | Visual polish pass — spacing, typography, micro-interactions |
| "сухо" | Add restrained polish, not color |
| "[page] тоже" / "везде" | Propagate change across all equivalent pages |
| "помни это" / "сохрани" | Write to auto-memory or propose CLAUDE.md addition |
| "деплой" | `ssh root@avalant.xyz "cd /root/wallet-monitor && ./scripts/deploy.sh"` |

---

## Decisions to NOT revisit (don't touch)

- **Admin is SQL-only**. No env-var path, no client path, no register flow grants admin.
- **`secure=True` on session cookies** in prod. Override only via `AVALANT_COOKIE_SECURE=0` for localhost dev.
- **WS token via first frame**, never URL.
- **TG-login bot replies with a button**, not just a confirmation message — mobile tabs freeze when TG opens.
- **Both bots can run independently** (auth + alerts). Single-bot deployments fall back gracefully.
- **CSRF tokens are intentionally absent** — Bearer auth, not cookie form-post.
- **Spot httpx client is dedicated**, never share with arb's `_http`.
- **Price-outlier filter for futures arb is OFF** per user request.
- **Light theme uses `#006B3C` green + `#8B0000` red**. Don't brighten.
- **Theme toggle button is disabled** (`theme.js` early-return). Don't re-enable without asking.
- **Screener has no left panel** — single column, exchanges-popover dropdown.
- **Freshness dots hidden globally**, only re-enabled in Alpha bottom strip.
- **Net/8h on `/arb` excludes price spread** (funding-only). Screener still computes `gross+spread-fees`.
- **Pricing + checkout stay open during portfolio maintenance** so users can renew.
- **`/admin → Maintenance` toggles are runtime**. No deploy needed.
- **Frontend is bind-mounted** — never copy frontend INTO image at build time only; compose mount must persist.
- **No CSRF**, no email comms (Telegram-only), no Stripe (CryptoCloud-only).
- **Admin is SQL-only**. Env-var auto-grant paths (`INITIAL_ADMIN_USERNAME`, `AVALANT_ALLOW_FIRST_USER_ADMIN`) are no longer honoured. TG-widget login also never grants admin.

---

## TODO highlights (see TODO.md)

- **Trade**: Limit/stop/TP orders, order history UI, partial fills, 5 missing adapters (Kraken, HTX-futures, Paradex, Lighter, Extended), position-size calculator, risk warnings
- **Storage HA**: DB+Redis on same host (SPOF), no PG read replica, logs not off-host, no off-site backups, no monthly restore-test
- **Portfolio**: cost-basis FIFO/LIFO, multi-currency, transaction CSV, hardware wallets, fallback price source for low-cap alts
- **Notifications**: only TG; no email, no in-app notification center, only spread alerts (no price-extremum)
- **Performance**: no frontend bundling/minification, no CDN for static, no PostgreSQL read replica
- **Compliance**: no GDPR data-export endpoint, no cookie-consent banner, no ToS/Privacy pages

ToS / Privacy / GDPR are required for EU-public release.
