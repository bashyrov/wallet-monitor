# Avalant — Development Guide for Claude

## What is this project

**Avalant** — a web application for aggregating crypto wallet balances from multiple sources. FastAPI backend + multi-page vanilla JS frontend. Database: PostgreSQL (production) / SQLite (local dev). Migrations: Alembic.

Brand: "avalant_" — Inter 800, 18px, with a blinking green `_`. Logo: `/avalant_favicon.svg` (icon), `/avalant-logo.svg` (full logo used in login/register forms).

Supported sources:
- **8 CEX exchanges**: Binance, OKX, Bybit, Gate, MEXC, KuCoin, Bitget, Backpack
- **5 Perp DEXes**: Hyperliquid, Aster, Lighter, Ethereal, Paradex (stub)
- **13 chains**: Tron, Ethereum, BSC, Polygon, Arbitrum, Optimism, Base, Avalanche, zkSync, Linea, Scroll, Mantle, Blast

---

## Running the project

### Local (SQLite)
```bash
source venv/bin/activate
uvicorn app:app --reload --port 8000
# Frontend: http://localhost:8000
# DB: wallet_monitor.db (created automatically via Alembic)
```

### Docker (PostgreSQL)
```bash
cp .env.sample .env   # fill in variables
docker compose up -d
# Frontend: http://localhost:8000
```

**First registered user** (lowest `id`) automatically gets `is_admin = true` and `plan = "unlim"`.

---

## Deployment

### First deploy on a server
```bash
cp .env.sample .env          # fill SECRET_KEY, ENCRYPTION_KEY, POSTGRES_PASSWORD
# Step 1: start nginx in HTTP-only mode (comment out 443 server block in nginx/nginx.conf)
make up
# Step 2: get SSL cert
make ssl-init DOMAIN=yourdomain.com EMAIL=your@email.com
# Step 3: uncomment 443 server block, replace yourdomain.com in nginx/nginx.conf
make restart-nginx
```

### Backup
```bash
./backup.sh           # pg_dump → backups/avalant_YYYYMMDD_HHMMSS.sql.gz (keeps 14 days)
./backup.sh 7         # keep last 7 days

# Restore
make restore FILE=backups/avalant_20260407_120000.sql.gz
```

**Cron (nightly at 3:00)**:
```
0 3 * * * cd /path/to/wallet-monitor && ./backup.sh >> /var/log/avalant-backup.log 2>&1
```

### Makefile commands
```bash
make dev              # local uvicorn --reload
make up / down        # docker compose up/down
make restart          # restart app container
make rebuild          # rebuild + restart app
make logs             # follow app logs
make backup           # PostgreSQL backup
make restore FILE=... # restore from backup
make ssl-init         # get Let's Encrypt cert (first time)
make restart-nginx    # reload nginx config
```

---

## Project structure

```
wallet-monitor/
├── app.py                              # FastAPI entry point: lifespan, CORS, security headers, routers,
│                                       #   _ensure_system_tags(), serve_page() handler (pages without .html)
├── settings.py                         # Pydantic BaseSettings — config from .env
├── requirements.txt
├── Dockerfile                          # python:3.13-slim, uvicorn
├── docker-compose.yml                  # PostgreSQL 16 + app + nginx + certbot (Let's Encrypt auto-renew)
├── Makefile                            # dev/deploy shortcuts
├── backup.sh                           # pg_dump with retention (default 14 days)
├── alembic.ini
├── alembic/
│   ├── env.py                          # Reads DATABASE_URL from settings, normalizes postgres:// → postgresql://
│   └── versions/
│       ├── 014613d42a04_initial.py     # Tables: users, wallets, tags, wallet_tags, wallet_addresses
│       ├── fb0ca8a11562_add_is_admin.py
│       ├── a1b2c3d4e5f6_add_is_archived.py
│       ├── c3d4e5f6a7b8_add_is_blocked_request_count.py
│       ├── d0e1f2a3b4c5_add_last_active_at.py
│       ├── e1f2a3b4c5d6_add_provider_error_logs.py
│       ├── e5f6a7b8c9d0_add_balance_snapshots.py
│       ├── f2a3b4c5d6e7_add_balance_history.py
│       ├── a2b3c4d5e6f7_tags_user_scoped.py   # tags.user_id (NULL = system tag)
│       └── g3h4i5j6k7l8_add_plan_to_users.py  # plan + plan_expires_at
│
├── frontend/
│   ├── auth.js                         # Shared auth module (getToken, setSession, requireAuth, requireAdmin, isAdmin, logout)
│   ├── avalant_favicon.svg             # Browser favicon (SVG)
│   ├── avalant_favicon.png             # Browser favicon (PNG, large original 2000×2000)
│   ├── avalant_favicon-48.png          # 48×48 — Google Search minimum requirement
│   ├── avalant_favicon-64.png          # 64×64
│   ├── avalant_favicon-96.png          # 96×96
│   ├── avalant_favicon-192.png         # 192×192 — apple-touch-icon
│   ├── avalant-logo.svg                # Full logo — used in login/register form cards
│   ├── favicon.ico                     # ICO fallback (16/32px)
│   ├── robots.txt                      # SEO: allow all
│   ├── sitemap.xml                     # SEO: all public pages
│   ├── og-image.jpg                    # Open Graph social preview image (1200×630)
│   ├── index.html                      # Landing page (public)
│   ├── app.html                        # Main app — portfolio, balances, transactions (auth required)
│   ├── profile.html                    # User profile, balance history chart, plan info, admin link (auth required)
│   ├── login.html                      # Login form → JWT + HttpOnly cookie → redirect to /app
│   ├── register.html                   # Register form → JWT + HttpOnly cookie → redirect to /app
│   ├── pricing.html                    # Pricing: Basic/Pro/Platinum/Enterprise with monthly/annual toggle
│   ├── checkout.html                   # Card payment form (stub)
│   ├── archive.html                    # Archived wallets with restore/delete (auth required)
│   ├── admin.html                      # Admin panel — KPI, users table with plan management, provider errors tab (admin required)
│   ├── admin-user.html                 # Per-user admin detail page (admin required)
│   ├── 404.html                        # Custom 404 page with terminal animation
│   └── maintenance.html               # Maintenance mode page
│
└── backend/
    ├── crypto.py                       # Fernet credential encryption: encrypt/decrypt_credentials()
    ├── plans.py                        # PLAN_LIMITS dict, VALID_PLANS, ADMIN_ONLY_PLANS, wallet_limit()
    │
    ├── db/
    │   ├── base.py                     # _make_engine() (SQLite + PostgreSQL), SessionLocal, Base, get_db()
    │   └── models.py                   # ORM: User, Wallet, Tag, wallet_tags (M2M), WalletAddress,
    │                                   #   BalanceSnapshot, ProviderErrorLog, BalanceHistory
    │
    ├── domain/
    │   ├── models.py                   # Dataclasses: WalletBasic, ExchangeWallet, ChainWallet, PerpDexWallet, BalanceResult
    │   ├── enums.py                    # ExchangeType, ChainType, PerpDexType
    │   └── errors.py                   # Domain exceptions: WalletNotFound, TagNotFound, InvalidProviderType, etc.
    │
    ├── schemas/
    │   ├── auth.py                     # UserRegister, UserLogin, Token, UserOut (includes is_admin, plan, plan_expires_at, wallet_limit)
    │   ├── common.py                   # TagCreate/Update/Out, WalletCreate, WalletOut, WalletAddressCreate/Out
    │   ├── portfolio.py                # BalanceFetchRequest, WalletBalanceResult, AggregatedBalance,
    │   │                               #   BalanceResponse, PnL, TransactionFetchRequest,
    │   │                               #   Transaction (with address field), TransactionResponse
    │   ├── wallets.py                  # ExchangeWalletSchema, ChainWalletSchema, PerpDexWalletSchema
    │   └── __init__.py                 # Re-exports from all three files
    │
    ├── providers/
    │   ├── base_wallet_provider.py     # ABC: fetch_balance(), _build_result(), _empty_details()
    │   │                               #   class attrs: name, label, enabled, needs_passphrase/needs_api_key
    │   ├── utils.py                    # STABLE_COINS tuple
    │   ├── exchanges/
    │   │   ├── __init__.py             # EXCHANGE_PROVIDERS dict {value → class}
    │   │   ├── _signing.py             # HMAC helpers: hex_hmac_sha256, b64_hmac_sha256, hex_hmac_sha512, ms(), s()
    │   │   ├── binance_provider.py     # HMAC-SHA256, python-binance AsyncClient + SAPI
    │   │   ├── okx_provider.py         # base64-HMAC-SHA256, server timestamp, passphrase
    │   │   ├── bybit_provider.py       # HMAC-SHA256, X-BAPI-* headers; UNIFIED + FUND + Earn locked products
    │   │   ├── gate_provider.py        # HMAC-SHA512, Gate.io v4; spot + USDT/BTC futures + Uni lending earn
    │   │   ├── kucoin_provider.py      # base64-HMAC-SHA256, server ts + passphrase; spot + futures (api-futures.kucoin.com)
    │   │   ├── mexc_provider.py        # HMAC-SHA256, spot + futures endpoints
    │   │   ├── bitget_provider.py      # base64-HMAC-SHA256, passphrase
    │   │   └── backpack_provider.py    # Ed25519 signature
    │   ├── perp_dexes/
    │   │   ├── __init__.py             # PERPDEX_PROVIDERS dict {value → class}
    │   │   ├── hyperliquid_provider.py # Public POST /info
    │   │   ├── aster_provider.py       # Aster DEX (soon=True)
    │   │   ├── lighter_provider.py     # Public GET /api/v1/account; HTTP 400 = unregistered addr (→ empty, not error)
    │   │   ├── ethereal_provider.py    # Public GET /v1/subaccount
    │   │   └── paradex_provider.py     # Starknet address
    │   └── chains/
    │       ├── __init__.py             # CHAIN_PROVIDERS dict + CHAIN_META {value → {label, enabled}}
    │       ├── base_chain_provider.py  # Base class: label, enabled, base_url
    │       ├── evm_chains.py           # EVMChainProvider (Ankr API + plain RPC fallback)
    │       └── tron_provider.py        # TronProvider (TronGrid + KNOWN_TRC20 mapping)
    │
    ├── api/
    │   ├── deps.py                     # get_db, get_current_user (JWT Bearer), get_admin_user (403 if not admin)
    │   └── v1/
    │       ├── router.py               # Main APIRouter prefix="/api", mounts all sub-routers
    │       ├── health.py               # GET /api/health
    │       ├── auth.py                 # POST /api/auth/register, /login, /logout; GET /api/auth/me; rate limiter
    │       ├── admin.py                # GET /api/admin/stats, /users, /users/{id}, /provider-errors
    │       │                           #   PATCH /api/admin/users/{id}/block|plan
    │       ├── wallets.py              # CRUD wallets, archive/unarchive, tags, addresses
    │       ├── tags.py                 # GET/POST/PUT/DELETE /api/tags; guards system tags (Owner)
    │       └── portfolio.py            # POST /balance, /transactions, /transactions/bulk
    │                                   #   GET /history
    │
    └── services/
        ├── auth_service.py             # register_user, authenticate_user, create_token, decode_token, get_user_by_*
        ├── wallet_service.py           # CRUD wallets, tags, wallet addresses + all_addresses()
        │                               #   wallet limit enforced via plans.wallet_limit(user.plan)
        ├── balance_service.py          # fetch_balances() → BalanceResponse; writes BalanceSnapshot + BalanceHistory
        │                               #   _fetch_single() returns 3-tuple (result, error, error_type)
        │                               #   writes ProviderErrorLog on failure
        ├── transaction_service.py      # fetch_transactions(db_wallet) → TransactionResponse (last 5 tx)
        ├── price_service.py            # get_usd_value(asset, amount) — CMC top-100 + Gate fallback, 30min cache
        ├── arbitrage_service.py        # Funding rate fetchers for 12 exchanges; two-tier cache (_cache 6s, _ivl_cache 5min)
        │                               #   get_funding_data() / get_arbitrage_opportunities() / get_cached_rates()
        │                               #   Aster: separate _aster_price_cache (5s TTL) + _aster_vol_cache (60s TTL)
        └── alert_service.py            # Background task (60s interval): checks arb spreads vs ArbAlert thresholds
                                        #   sends Telegram message via TG_BOT_TOKEN; cooldown 1h per alert
```

---

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/health` | — | `{"status": "ok"}` |
| POST | `/api/auth/register` | — | Register → `{access_token}` + sets HttpOnly `session` cookie |
| POST | `/api/auth/login` | — | Login → `{access_token}` + sets HttpOnly `session` cookie |
| POST | `/api/auth/logout` | — | Deletes `session` cookie |
| GET | `/api/auth/me` | Bearer | Current user (`id, username, email, is_admin, plan, plan_expires_at, wallet_limit, tg_username`) |
| PATCH | `/api/auth/me` | Bearer | Update `tg_username` — `{tg_username: "handle"}` |
| GET | `/api/admin/stats` | Bearer + admin | KPI: users_count, wallets_count, by_type, recent_users |
| GET | `/api/admin/users` | Bearer + admin | All users with wallet_count, plan, plan_expires_at, wallet_limit, request_count, last_active_at, is_blocked |
| GET | `/api/admin/users/{id}` | Bearer + admin | Single user detail with plan info |
| GET | `/api/admin/provider-errors?n=500` | Bearer + admin | Aggregated error counts by provider + error_type |
| PATCH | `/api/admin/users/{id}/block` | Bearer + admin | Toggle is_blocked (cannot block self) |
| PATCH | `/api/admin/users/{id}/plan` | Bearer + admin | Set plan + optional plan_expires_at; `unlim` only assignable to admins |
| GET | `/api/wallets` | Bearer | Current user's active wallets |
| POST | `/api/wallets` | Bearer | Create wallet (enforces plan limit on backend) |
| DELETE | `/api/wallets/{id}` | Bearer | Delete wallet |
| GET | `/api/wallets/archived` | Bearer | Archived wallets |
| POST | `/api/wallets/{id}/archive` | Bearer | Archive wallet |
| POST | `/api/wallets/{id}/unarchive` | Bearer | Unarchive wallet (enforces plan limit on backend) |
| POST | `/api/wallets/{id}/tags/{tag_id}` | Bearer | Add tag to wallet |
| DELETE | `/api/wallets/{id}/tags/{tag_id}` | Bearer | Remove tag |
| GET | `/api/wallets/options` | Bearer | Available types (exchange/chain/perpdex lists) |
| GET | `/api/wallets/all-addresses` | Bearer | All named addresses + chain/perpdex addresses (for address book) |
| GET | `/api/wallets/{id}/addresses` | Bearer | Named addresses for a wallet |
| POST | `/api/wallets/{id}/addresses` | Bearer | Add named address `{name, address}` |
| DELETE | `/api/wallets/{id}/addresses/{addr_id}` | Bearer | Delete named address |
| GET | `/api/tags` | Bearer | Tag list |
| POST | `/api/tags` | Bearer | Create tag (cannot use reserved names: "Owner") |
| PUT | `/api/tags/{id}` | Bearer | Update tag (cannot modify system tags) |
| DELETE | `/api/tags/{id}` | Bearer | Delete tag (cannot delete system tags) |
| POST | `/api/portfolio/balance` | Bearer | Balances `{"wallet_ids": [1,2,3]}` — empty list = all |
| POST | `/api/portfolio/transactions` | Bearer | Last 5 tx for one wallet `{"wallet_id": 1}` |
| POST | `/api/portfolio/transactions/bulk` | Bearer | Last 5 tx for all (or selected) wallets in parallel |
| GET | `/api/portfolio/history?days=30` | Bearer | Balance history for chart (1–365 days) |
| GET | `/api/alerts` | Bearer | List user's arb spread alerts |
| POST | `/api/alerts` | Bearer | Create alert `{symbol, long_exchange, short_exchange, threshold, direction}` |
| PATCH | `/api/alerts/{id}` | Bearer | Update alert |
| PATCH | `/api/alerts/{id}/toggle` | Bearer | Toggle alert enabled/disabled |
| DELETE | `/api/alerts/{id}` | Bearer | Delete alert |
| GET | `/api/screener/funding` | Bearer | All funding rates (cached 6s per exchange) |
| GET | `/api/screener/arbitrage` | Bearer | Cross-exchange arb opportunities with net P&L |
| GET | `/api/screener/orderbook?symbol=&exchange=&limit=` | Bearer | Order book for a symbol on an exchange |
| GET | `/api/screener/arb-price-history?symbol=&long_ex=&short_ex=` | Bearer | 1h OHLCV klines for two exchanges |
| GET | `/api/screener/arb-history?symbol=&long_ex=&short_ex=` | Bearer | Funding rate history for two exchanges |
| GET | `/api/screener/all-exchanges-funding?symbol=` | Bearer | Current funding rate for symbol across all exchanges |
| GET | `/api/screener/open-interest?symbol=&long_ex=&short_ex=` | Bearer | Open interest for both exchanges |
| WS | `/api/screener/ws/funding?token=` | Bearer (query) | Live funding rates pushed every 5s |
| WS | `/api/screener/ws/arb?token=` | Bearer (query) | Live arb opportunities pushed every 5s |

---

## Authentication

JWT Bearer tokens (`python-jose`, HS256). Passwords — bcrypt (`passlib[bcrypt]`, `bcrypt>=4,<5`).

```
POST /api/auth/register {username, email, password}
  → auth_service.register_user(db, username, email, password)
    → bcrypt.hash(password) → User(is_admin=True, plan="unlim" if first user, else plan="basic")
    → set HttpOnly "session" cookie + return Token(access_token=create_token(user.id))

POST /api/auth/login {login, password}   # login = username OR email
  → auth_service.authenticate_user(db, login, password)
  → bcrypt.verify(password, hashed) → set cookie + Token

POST /api/auth/logout
  → delete "session" cookie

GET /api/auth/me   Authorization: Bearer <token>
  → deps.get_current_user → decode_token → User
```

**Rate limiting on `/api/auth/*`**: in-memory, per IP, 10 attempts / 60 sec → 429. Cleared on successful login. `X-Forwarded-For` supported.

**HttpOnly session cookie**: set on login/register, used by `serve_page()` in `app.py` for backend page protection. Frontend still uses Bearer token from localStorage for API calls.

---

## Plan System (`backend/plans.py`)

```python
PLAN_LIMITS = {
    "basic":      4,      # free, default for all new users
    "pro":        30,     # $5/mo or $48/yr
    "platinum":   70,     # $10/mo or $96/yr
    "enterprise": None,   # custom/unlimited, from $10/mo
    "unlim":      None,   # unlimited, admin-only
}
```

- **Default plan**: `basic` (4 wallets) for all new registrations
- **First user** (admin): automatically gets `unlim`
- **`unlim`** can only be assigned to users who are already admins
- Wallet limit enforced on backend in `wallet_service.create_wallet()` and `unarchive_wallet()` via `wallet_limit(user.plan)`
- `wallet_limit` is exposed in `UserOut` (computed via `@model_validator` in `schemas/auth.py`) — frontend reads it from `/api/auth/me`; no hardcoded limits in JS
- Admin sets plan via `PATCH /api/admin/users/{id}/plan {plan, plan_expires_at}`
- `plan_expires_at` is stored but not automatically enforced (manual management)
- **To add a new plan**: add entry to `PLAN_LIMITS` in `plans.py` — everything else (API, frontend popup, admin modal) picks it up automatically

**Annual pricing** (20% discount, rounded):
- Pro: $5/mo → $48/yr ($4/mo effective)
- Platinum: $10/mo → $96/yr ($8/mo effective)

---

## Database

PostgreSQL (production) / SQLite (local). Migrations run automatically on startup (`alembic upgrade head`).

### Table `users`
| Field | Type | Description |
|-------|------|-------------|
| id | Integer PK | |
| username | String UNIQUE | |
| email | String UNIQUE | |
| hashed_password | String | bcrypt |
| is_admin | Boolean | default False; first user → True |
| is_blocked | Boolean | default False; blocked users cannot login |
| plan | String | `basic` \| `pro` \| `platinum` \| `enterprise` \| `unlim`; default `basic` |
| plan_expires_at | DateTime nullable | date until which the paid plan is active |
| request_count | Integer | incremented on balance + transaction API calls |
| last_active_at | DateTime nullable | updated on each balance/transaction request |
| tg_username | String nullable | Telegram handle (without @) for alert notifications |
| created_at | DateTime | |

### Table `wallets`
| Field | Type | Description |
|-------|------|-------------|
| id | Integer PK | |
| name | String | min 6 chars |
| wallet_type | String | `exchange` / `chain` / `perpdex` |
| type_value | String | `binance`, `tron`, `hyperliquid`, ... |
| credentials | JSON | **Fernet-encrypted** values: `{api_key, api_secret, api_passphrase?}` or `{address}` |
| is_archived | Boolean | soft delete — archived wallets hidden from main view |
| user_id | Integer FK → users | |
| created_at | DateTime | |

### Table `tags`
| Field | Type | Description |
|-------|------|-------------|
| id | Integer PK | |
| name | String | unique per user |
| color | String | hex `#RRGGBB` |
| user_id | Integer FK nullable | NULL = system tag (Owner); otherwise scoped to user |

### Table `wallet_tags` (M2M)
wallet_id + tag_id, CASCADE DELETE

### Table `wallet_addresses`
| Field | Type | Description |
|-------|------|-------------|
| id | Integer PK | |
| wallet_id | Integer FK → wallets | CASCADE DELETE |
| name | String | User label, e.g. "Binance SOL" |
| address | String | On-chain address |
| created_at | DateTime | |

### Table `balance_snapshots`
| Field | Type | Description |
|-------|------|-------------|
| id | Integer PK | |
| wallet_id | Integer FK UNIQUE | one row per wallet |
| user_id | Integer FK | |
| totals | JSON | `{"USDT": "1234.56", "BTC": "0.5"}` |
| stable_total | Float | pre-computed stablecoin USD sum (for PnL) |
| snapshot_at | DateTime | |

### Table `provider_error_logs`
| Field | Type | Description |
|-------|------|-------------|
| id | Integer PK | |
| wallet_type | String | `exchange` / `chain` / `perpdex` |
| type_value  | String | `binance`, `ethereum`, etc. |
| error_type  | String | `rate_limit` / `auth` / `network` / `unknown` |
| created_at | DateTime index | |

### Table `balance_history`
| Field | Type | Description |
|-------|------|-------------|
| id | Integer PK | |
| user_id | Integer FK index | |
| usd_total | Float | aggregate USD of all Owner-tagged wallets |
| snapshot_at | DateTime index | written on each balance check with Owner wallets |

### Table `arb_alerts`
| Field | Type | Description |
|-------|------|-------------|
| id | Integer PK | |
| user_id | Integer FK index | CASCADE DELETE |
| symbol | String | e.g. `BTC` (without USDT) |
| long_exchange | String | exchange name lowercase |
| short_exchange | String | exchange name lowercase |
| threshold | Float | min spread % to trigger (e.g. `0.05`) |
| direction | String | `any` \| `above` \| `below`; default `any` |
| enabled | Boolean | default True |
| last_triggered_at | DateTime nullable | used for 1h cooldown |
| created_at | DateTime | |

---

## Credential Encryption (`backend/crypto.py`)

All string values in the `credentials` JSON are Fernet-encrypted on save and decrypted on read.

- Key: PBKDF2-SHA256 from `settings.ENCRYPTION_KEY`, 260,000 iterations, salt = `b"wallet-monitor-creds-v1"`
- `encrypt_credentials(creds: dict) → dict` — encrypts all str values
- `decrypt_credentials(creds: dict) → dict` — decrypts, graceful fallback to plain text (legacy)
- `WalletOut.display_info` — masked representation (e.g. `abcd****wxyz`), credentials are never returned in the API

---

## Security

### app.py
- **CORS**: `CORSMiddleware`, configured via `ALLOWED_ORIGINS` (comma-separated or empty = same-origin only)
- **Security headers**: `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `X-XSS-Protection: 1; mode=block`, `Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy`; removes `Server` header
- **OpenAPI hidden**: `docs_url=None, redoc_url=None, openapi_url=None`
- **`_check_security()`**: warns on startup if default `SECRET_KEY` / `ENCRYPTION_KEY` are used
- **`_ensure_system_tags()`**: creates the "Owner" tag (color `#1AFFAB`) on startup if it doesn't exist
- **`serve_page()`**: serves all pages without `.html` extension; checks HttpOnly `session` cookie for protected pages; redirects unauthenticated users to `/login?next=/page`

### Access control
- **`get_current_user`** (`deps.py`): validates Bearer JWT, 401 if missing/invalid, 403 if `is_blocked`
- **`get_admin_user`** (`deps.py`): wraps `get_current_user`, 403 if `is_admin=False`
- **Backend page protection** (`serve_page` in `app.py`): `_AUTH_PAGES = {"app", "profile", "archive", "screener", "arb"}`, `_ADMIN_PAGES = {"admin", "admin-user"}` — checked via HttpOnly `session` cookie
- **Frontend** (`auth.js`): `requireAuth()` → redirect to `/login`; `requireAdmin()` → redirect to `/app`

### URL routing (no .html)
All pages are served without `.html` extension:
- `/app`, `/profile`, `/archive`, `/admin`, `/admin-user`, `/login`, `/register`, `/pricing`, `/checkout`
- `serve_page` handler in `app.py` maps `/{page}` → `frontend/{page}.html`
- Static files (`.js`, `.svg`, `.css`, etc.) are served directly from `frontend/`

---

## System Tags

`SYSTEM_TAGS = {"Owner"}` defined in `backend/api/v1/tags.py`.

Rules enforced on the backend:
- Cannot create a tag with a reserved name
- Cannot update or delete a system tag (`_guard_system()` → HTTP 400)

The **Owner** tag is auto-created on every startup (`_ensure_system_tags()` in `app.py`). It is a system tag with `user_id = NULL`.

**Purpose**: wallets tagged "Owner" are included in balance history snapshots. The aggregate USD total of all Owner-tagged wallets is written to `balance_history` after each balance check.

Frontend displays Owner tag with `◈ Owner` green pill and no delete button. The "system" label appears instead of the delete action in the manage-tags view.

---

## How the provider system works

### Balances
```
POST /api/portfolio/balance
  → balance_service.fetch_balances(db_wallets, db)
    → asyncio.gather(_fetch_single(w) for w in wallets)   # return_exceptions=True
      → _fetch_single(db_wallet) → (BalanceResult|None, error_str|None, error_type|None):
          1. decrypt_credentials(db_wallet.credentials)
          2. Validate via Pydantic (ExchangeWalletSchema / ChainWalletSchema / ...)
          3. Create domain object (ExchangeWallet / ChainWallet / ...)
          4. domain.__post_init__ → _resolve_provider() → looks up in PROVIDERS dict
          5. provider = wallet.provider()  # creates instance
          6. await provider.fetch_balance(wallet) → BalanceResult
          7. aclose() in finally
          error_type: 'rate_limit'|'auth'|'network'|'unknown'

    After gathering:
    - per wallet: upsert BalanceSnapshot, compute PnL vs previous snapshot
    - on error: write ProviderErrorLog row
    - if any Owner-tagged wallets: write BalanceHistory row (aggregate usd_total)
    - compute AggregatedBalance across all wallets
```

### Transactions (single)
```
POST /api/portfolio/transactions
  → transaction_service.fetch_transactions(db_wallet)
    → dispatch by wallet_type / type_value
      → provider-specific async function (_binance_txs, _okx_txs, ...)
        → decrypt_credentials → returns list[Transaction] (max 5)
```

### Transactions (bulk)
```
POST /api/portfolio/transactions/bulk
  → asyncio.gather(fetch_transactions(w) for w in wallets)   # parallel
  → on error per wallet: ProviderErrorLog written, empty TransactionResponse returned
```

### BalanceResult
```python
@dataclass
class BalanceResult:
    wallet: ExchangeWallet | ChainWallet | PerpDexWallet
    provider: str
    totals: dict | None    # {"USDT": "1234.56", "BTC": "0.5"}
    details: dict | None   # {"spot": {...}, "futures": {...}, "earn": {...}}
```

### PnL (in BalanceResponse)
```python
class PnL(BaseModel):
    prev: str        # previous stable total
    abs: str         # absolute change e.g. "+120.34"
    pct: str         # percent change e.g. "+4.32"
    direction: str   # "up" | "down" | "flat"
```
PnL is computed per-wallet (vs `balance_snapshots.stable_total`) and aggregated across all wallets.

### Transaction
```python
class Transaction(BaseModel):
    tx_id: str
    type: str       # deposit / withdraw / trade / fill / transfer / contract
    asset: str
    amount: str
    timestamp: str  # "YYYY-MM-DD HH:MM"
    status: str     # completed / pending / failed
    address: str | None  # counterparty address (from/to) — for address book matching
```

---

## Provider metadata system

Each provider is a class with attributes that automatically populate `/api/wallets/options`.

**Class attributes:**
```python
class BinanceProvider(BaseWalletProvider):
    name = "BinanceProvider"   # internal ID
    label = "Binance"          # displayed in UI
    enabled = True             # False → hidden from UI and not used
    needs_passphrase = False   # for exchange providers
    needs_api_key = False      # for perpdex providers
    soon = True                # for perpdex — shows "soon" badge
```

**Chain providers** share a single class for all EVM networks, so metadata lives in `CHAIN_META` (`chains/__init__.py`):
```python
CHAIN_META: dict[str, dict] = {
    "ethereum": {"label": "Ethereum", "enabled": True},
    # enabled=False → chain hidden from UI
}
```

**`WALLET_OPTIONS`** in `wallets.py` is generated automatically from providers via `_build_wallet_options()`. No manual editing needed — just change the class attributes.

---

## Adding a new provider

### New exchange
1. Create `backend/providers/exchanges/newexchange_provider.py`, inherit `BaseWalletProvider`
2. Set class attrs: `name`, `label`, `enabled = True`, `needs_passphrase`
3. Implement `fetch_balance(wallet) → BalanceResult`, use `self._build_result(...)`
4. Register in `EXCHANGE_PROVIDERS` in `backend/providers/exchanges/__init__.py`
5. Add to `ExchangeType` in `backend/domain/enums.py`
6. Add `_newexchange_txs(creds)` function in `transaction_service.py` and wire into dispatcher
7. ~~Edit `wallets.py`~~ — not needed, options are auto-generated

### New chain
1. Create provider in `backend/providers/chains/`, inherit `BaseChainProvider`
2. Register in `CHAIN_PROVIDERS` in `backend/providers/chains/__init__.py`
3. Add entry to `CHAIN_META` (`{"label": "Name", "enabled": True}`)
4. Add to `ChainType` enum
5. Add RPC URL to `settings.py` as `str | None = None`

### New Perp DEX
1. Create `backend/providers/perp_dexes/new_provider.py`
2. Set class attrs: `name`, `label`, `enabled = True`, `needs_api_key`, `soon` (if needed)
3. Register in `PERPDEX_PROVIDERS` in `backend/providers/perp_dexes/__init__.py`
4. Add to `PerpDexType` enum
5. Add `_newdex_txs(address)` function in `transaction_service.py`

### Disable a provider
Set `enabled = False` on the class (exchange/perpdex) or `"enabled": False` in `CHAIN_META` (chain). The provider disappears from the UI and is excluded from `/api/wallets/options`.

---

## Environment variables

```env
# Database
DATABASE_URL=postgresql://user:pass@localhost:5432/avalant  # SQLite by default

# Security (MUST override in production!)
SECRET_KEY=change-me-in-production-use-a-long-random-string
ENCRYPTION_KEY=change-me-in-production-use-a-long-random-string
ACCESS_TOKEN_EXPIRE_DAYS=30

# CORS (empty = same-origin only)
ALLOWED_ORIGINS=https://yourdomain.com,https://www.yourdomain.com

# Logging
LOG_LEVEL=INFO   # DEBUG | INFO | WARNING | ERROR | CRITICAL

# EVM RPC URLs (only needed without ANKR_KEY)
ETHEREUM_RPC=   BSC_RPC=       POLYGON_RPC=   ARBITRUM_RPC=
OPTIMISM_RPC=   BASE_RPC=      AVALANCHE_RPC= ZKSYNC_RPC=
LINEA_RPC=      SCROLL_RPC=    MANTLE_RPC=    BLAST_RPC=

# Ankr — provides all ERC-20 token balances for all EVM chains (recommended)
ANKR_KEY=

# CoinMarketCap — top-100 token list for price cache (refreshed every 30 min)
CMC_API_KEY=

# Tron — TronGrid Pro key (optional, increases rate limits)
TRON_KEY=
TRON_RPC=

# Solana
SOLANA_RPC=

# Telegram bot token — for spread alert notifications (BotFather)
TG_BOT_TOKEN=
```

**EVM balances**: `ANKR_KEY` → `ankr_getAccountBalance` (all tokens). Without it → `eth_getBalance` (native only).
**EVM transactions**: `ANKR_KEY` → `ankr_getTokenTransfers` → `ankr_getTransactionsByAddress` (fallback). Without it — empty list.
**Token prices**: `CMC_API_KEY` → top-100 list, then `get_usd_value()` fills in via Gate spot prices. Without it — only stablecoin totals shown.

---

## Frontend

Multiple self-contained HTML pages (inline CSS + JS). Shared design language. All pages use `auth.js` for authentication. All URLs served **without `.html`** extension via `serve_page()` in `app.py`.

### Pages
| File | Auth guard | Description |
|------|-----------|-------------|
| `index.html` | — | Landing page |
| `app.html` | requireAuth + backend cookie | Main app: wallet list, balance check, transactions view |
| `profile.html` | requireAuth + backend cookie | Profile: balance history chart, plan info with color badge, Telegram username field, admin link |
| `login.html` | redirectIfAuthed | Login form → JWT + HttpOnly cookie → redirect to /app |
| `register.html` | redirectIfAuthed | Register form → JWT + HttpOnly cookie → redirect to /app |
| `pricing.html` | — | Basic/Pro/Platinum/Enterprise plans, monthly/annual toggle |
| `checkout.html` | — | Card payment form (stub) |
| `archive.html` | requireAuth + backend cookie | Archived wallets with restore/delete; fetches `/auth/me` for wallet_limit |
| `admin.html` | requireAdmin + backend cookie | KPI, users table with plan badge + plan modal, provider errors tab |
| `admin-user.html` | requireAdmin + backend cookie | Per-user detail: stats, wallet list, last active |
| `screener.html` | requireAuth + backend cookie | Funding rate screener: sortable table, WebSocket live updates, ↗ link to arb detail |
| `arb.html` | requireAuth + backend cookie | Arb pair detail: 3-column terminal layout (charts / order books / P&L) |
| `404.html` | — | Custom 404 with terminal animation |
| `maintenance.html` | — | Maintenance mode page |

### auth.js — API
```javascript
Auth.getToken()                   // Bearer token from localStorage
Auth.getUser()                    // Decoded user {id, username, email, is_admin}
Auth.setSession(token, user)      // Save to localStorage
Auth.clearSession()               // Clear localStorage
Auth.isLoggedIn()                 // Check for token presence
Auth.isAdmin()                    // is_admin from saved user
Auth.requireAuth(redirect)        // Redirect to /login if not logged in
Auth.requireAdmin(redirect)       // Redirect to /app if not admin
Auth.redirectIfAuthed(redirect)   // Redirect if already logged in
Auth.logout()                     // POST /api/auth/logout (delete cookie) + clearSession + redirect
Auth.apiFetch(url, opts)          // fetch with Bearer header — prepends /api automatically
```

**Important**: `Auth.apiFetch` prepends `/api` internally. Pass paths without `/api` prefix: `'/auth/me'` not `'/api/auth/me'`.

### Design system
```css
--bg:       #0E0E11   /* main background */
--surface:  #131217   /* cards */
--surface2: #17171C   /* nested elements */
--surface3: #202028   /* hover states */
--border:   #22222A   /* all borders */
--text:     #E6E8E3   /* primary text */
--text2:    #9B9FAB   /* secondary */
--text3:    #676B7E   /* muted */
--green:    #1AFFAB   /* accent, CTA, positive */
--teal:     #06B6D4   /* chain type */
--purple:   #925BD6   /* perpdex type */
--yellow:   #E5C07B   /* exchange type */
--red:      #F87171   /* errors, negative */
```

Fonts: **Inter** (Google Fonts) — all UI; **JetBrains Mono** — numbers, addresses, amounts.

### app.html — JS structure
```javascript
// Wallet limit is now enforced on the backend by user.plan.
// Frontend reads limit from API response on wallet create (402 = limit reached).

S = {
  wallets, tags, options,
  selected: Set,          // selected wallet_ids for balance check
  tagFilter,              // active tag filter
  tagDropOpen,            // wallet_id with open tag dropdown (or null)
  walletType,             // 'exchange' | 'chain' | 'perpdex' | null — open accordion section
  addrPanelOpen,          // wallet_id with open address panel (or null)
  addressBook,            // { "0xabc...": { label, walletName, walletType } } — for tx highlighting
  view,                   // 'balances' | 'transactions' — current results panel view
  results,                // balance results
  txResults,              // transaction results (bulk)
  loadingBalance,
  loadingTx,
  lastChecked,
  _progress,              // 0–100 progress for loader
  _progressLabel,         // "Checking balances..." etc.
  _progressSub,           // sub-label e.g. "3 / 7"
  _progressTimer,         // setInterval reference
}

S_txMode = 'wallet' | 'time'   // transaction grouping mode (module-level, not in S)

api.get/post/del/put()          // Auth.apiFetch wrappers to /api/*
init()                          // loads wallets + tags + options in parallel, then loadAddressBook()
loadAddressBook()               // GET /api/wallets/all-addresses → builds S.addressBook
renderAll()                     // renderTagFilters() + renderWallets()
renderResults()                 // shimmer → token grid sorted by USD desc, allocation %
checkBalance()                  // POST /api/portfolio/balance → S.view = 'balances'
checkTransactions()             // POST /api/portfolio/transactions/bulk → S.view = 'transactions'
renderTxView()                  // renders tx panel with mode switcher (By wallet / By time)
_txByWallet()                   // groups transactions by wallet, each wallet collapsible
_txByTime()                     // merges all tx across wallets, sorted by timestamp desc, wallet badge on each row
_txRow(tx, opts)                // renders single tx row; addr-match shows wallet name as colored type badge
_txIcon(type)                   // returns SVG icon for tx type

toggleTxPanel(walletId)         // single-wallet tx accordion (inside balance view)
toggleAddrPanel(e, walletId)    // opens inline address panel for wallet
addWalletAddr(walletId)         // POST /api/wallets/{id}/addresses
delWalletAddr(walletId, addrId) // DELETE /api/wallets/{id}/addresses/{addr_id}
togglePanel()                   // collapse/expand left wallet panel (desktop)
toggleMobileWalletList()        // collapse/expand wallet list on mobile
openAddWalletModal()            // opens add modal; 402 from backend → openUpgradePopup()

// Add Wallet Modal — accordion
selectWalletType(type)          // opens the correct accordion section (exchange/chain/perpdex)
renderProtoGrid(id, name, options, cb)  // renders protocol chip selectors instead of <select>
_resetProtoChips()              // resets chip selection when modal opens

// Tag dropdown — rendered as a fixed portal in document.body
openTagDrop(e, id)              // positions dropdown via getBoundingClientRect(), appends to body
tagDropdown(w, rect)            // creates #tag-drop-portal with position:fixed — avoids overflow clipping
closeTagDrop()                  // removes portal element

// Confirm popup (universal)
openConfirm({title, sub, name, onConfirm})  // custom popup instead of confirm()
closeConfirm()
```

### Token grid in balance results
- Stablecoins and other tokens merged into a single sorted grid
- Sorted by USD value descending (largest allocation first)
- Each cell shows: token name, amount, USD value, allocation % (top-right corner, `position:absolute`)
- `document.title` updated to `"$12,345 · Avalant"` after balance check

### Progress loader
Shown during balance check (parallel) and bulk transaction fetch (sequential).
- Large centered `%` counter with ease animation
- Glowing progress bar
- 12 floating dots with random `@keyframes fp-float` animations (CSS only, no wallet list)
- Balance: smooth fake progress easing to 88%, jumps to 100% on completion
- Transactions: real `N / total` progress (sequential fetch)

### Add Wallet Modal
Three-section accordion (Exchange / Chain / Perp DEX). Clicking a section header opens it and closes the previous one. Protocol selection uses clickable chips (`proto-chip`) instead of `<select>`. Hidden `<input type="hidden" name="exchange_type|chain_type|perpdex_type">` holds the current selection. Accent color changes by type: yellow / teal / purple.

Address validation on submit:
- EVM: `/^0x[0-9a-fA-F]{40}$/i`
- Tron: `/^T[1-9A-HJ-NP-Za-km-z]{33}$/`
- Starknet (Paradex): `/^0x[0-9a-fA-F]{1,64}$/i`
- API key/secret: printable ASCII, min 8 chars

### Custom confirm popups
All destructive actions use `openConfirm()` instead of native `confirm()`:
- Delete wallet (`delWallet`)
- Delete tag (`delTag`)
- Delete from archive (`deleteWallet` in `archive.html`)

### UI effects in app.html
- Left wallet panel: `width: 360px → 0` with `transition: width 0.32s cubic-bezier(0.16,1,0.3,1)`
- Panel toggle button: green, SVG arrow rotates 180° on collapse
- Mobile: collapse button at top of wallet list, hides list via `max-height` transition
- Wallet rows: stagger animation `animation-delay: ${idx * 30}ms`
- Result cards: `border-left: 2px solid` with type color (yellow/teal/purple)
- Balance counter: ease-out-quart animation from 0 to value
- TX address match: colored wallet-type badge showing wallet name + optional label suffix
- Upgrade popup: on 402 from backend (limit reached), progress bar + link to /pricing
- Type accordion: `.acc-content` max-height 0→700px, cubic-bezier animation

### Password visibility toggle
Login and register forms have eye-icon toggle buttons in password fields (`togglePw(id, btn)`). Icon switches between open-eye and crossed-eye SVG.

### profile.html — Balance History Chart
Section order: Balance History chart → Subscription Plan → Wallet Breakdown → Danger Zone.

```javascript
loadHistory(days, btn)   // GET /api/portfolio/history?days=N → calls buildChart(data)
buildChart(data)         // renders SVG line chart
_renderPlanCard(me)      // renders plan badge with color, name, desc, expiry, usage bar
```

SVG chart features:
- `viewBox="0 0 600 160"` + `preserveAspectRatio="xMidYMid meet"` — mobile-responsive, no JS resize needed
- Cubic bezier smooth curves: symmetric control points `cx = (x0+x1)/2`
- `linearGradient` fill from `#1AFFAB22` to transparent
- `clipPath` with `rx=6` — rounded corners
- Y-axis labels (min/max), X-axis date labels, last-value label top-right
- Dot markers when ≤30 data points
- Empty state: "No history yet — tag wallets with ◈ Owner and check balance"
- Range buttons: 7d / 30d / 90d

Plan card colors by plan:
- basic → `#676B7E` (grey), pro → `#3B82F6` (blue), platinum → `#925BD6` (purple)
- enterprise → `#E5C07B` (yellow), unlim → `#1AFFAB` (green)

### admin.html — Plan management
Users table columns: Username · Email · Wallets/Limit · Requests · **Plan** · **Expires** · Role · Status · Last active · Actions

- **Plan badge**: colored pill per plan (`.plan-basic`, `.plan-pro`, `.plan-platinum`, `.plan-enterprise`, `.plan-unlim`)
- **Plan button**: opens plan modal (`openPlanModal(userId, username, currentPlan, isAdmin, currentExpires)`)
- **Plan modal**: grid of plan options, date input for expiry, Save calls `PATCH /api/admin/users/{id}/plan`
- `unlim` option only shown when target user is admin
- After save: badge and expires cell updated in-place without full reload

---

## Transactions (transaction_service.py)

Each provider has its own async function. Strategy: deposits/withdrawals first, trading activity as fallback.

| Provider | Sources (priority) | Auth |
|----------|-------------------|------|
| Binance | spot deposits + withdrawals + futures income (`/fapi/v1/income`) | HMAC-SHA256 |
| OKX | asset deposit-history + withdrawal-history + trade fills | base64-HMAC + server ts + passphrase |
| Bybit | deposit query-record + withdraw query-record + transaction-log | HMAC-SHA256 X-BAPI-* |
| Gate | wallet deposits + withdrawals + spot trades + futures book (USDT+BTC settle) | HMAC-SHA512 |
| KuCoin | `/api/v1/deposits` + `/api/v1/withdrawals` + ledgers (pageSize≥10) | base64-HMAC + server ts + passphrase |
| MEXC | deposit hisrec + withdraw history | HMAC-SHA256 |
| Bitget | spot account bills + USDT/USDC futures bills | base64-HMAC + passphrase |
| Backpack | deposits + withdrawals + fills history | Ed25519 |
| Hyperliquid | `POST /info {"type": "userFills"}` | Public |
| Lighter | order-history (fills 403) | Public |
| Ethereal | fills via subaccount_id + orders fallback | Public |
| EVM chains | `ankr_getTokenTransfers` (primary) → `ankr_getTransactionsByAddress` (fallback) | ANKR_KEY |
| Tron | `/v1/accounts/{addr}/transactions/trc20` | TRON_KEY (optional) |

Transactions are normalized into the `Transaction` model. Cached on the frontend in `_txCache[walletId]`.
The `address` field (counterparty) is populated where available: Binance deposits/withdrawals, OKX, Bybit, EVM, Tron.

---

## Address Book (named addresses)

Allows attaching arbitrary on-chain addresses to any wallet with a custom label.

**Typical use-case**: add a Binance Solana deposit address as "Binance SOL" → when viewing transactions on a Solana wallet, incoming transfers from that address are highlighted green with the label "Binance SOL".

**How matching works**:
1. `GET /api/wallets/all-addresses` returns: named addresses from `wallet_addresses` + addresses from chain/perpdex wallet credentials
2. Frontend builds `S.addressBook` (map `address.toLowerCase() → { label, walletName, walletType }`)
3. When rendering transactions: if `tx.address` (counterparty) is in addressBook → colored wallet-type badge showing wallet name

---

## Admin panel (admin.html)

Tabs: Overview · All Users · Provider Errors

**Provider Errors tab** (`GET /api/admin/provider-errors?n=500`):
- Window selector: 100/500/1000/5000/10000 last rows
- KPI cards per error type (rate_limit / auth / network / unknown)
- Table: provider, type, error counts with mini bar charts
- Helps identify consistently failing providers

**All Users table**: plan badge, wallet count vs limit, plan expiry, block/unblock, plan change button.

---

## Roadmap

- [ ] **Solana provider** — `SOLANA_RPC` in settings exists, `_solana_txs()` stub in transaction_service, balance provider not written
- [ ] **Notifications** — alert if balance changes > N%
- [ ] **Edit wallet** — currently only create and delete
- [ ] **Export** — CSV/JSON balance export
- [ ] **Search** — wallet search in left panel
- [ ] **Fantom** — exists in `CHAIN_PROVIDERS` but not in `CHAIN_META` → add entry to `CHAIN_META` to show in UI
- [ ] **Payment system** — checkout.html is a stub; plan_expires_at not auto-enforced yet
- [ ] **Auto plan expiry** — currently `plan_expires_at` is stored but not enforced; needs scheduled job

---

## Important gotchas

1. **`wallet.provider`** is a **class**, not an instance. `wallet.provider()` creates the instance.
2. **Passphrase** required only for: OKX, KuCoin, Bitget. Indicated by `needs_passphrase = True` on the class.
3. **TRX decimals** — 6 (SUN). USDT TRC20 — also 6. USDD — 18.
4. **`credentials` in DB** — Fernet-encrypted. Key derived from `ENCRYPTION_KEY` via PBKDF2.
5. **Double `aclose()`** — some providers call `aclose()` inside `fetch_balance`. httpx handles repeated close without errors.
6. **`name` validation** — minimum 6 characters (in `WalletCreate` and `WalletBasicSchema`).
7. **`return_exceptions=True`** in `asyncio.gather` — one provider error does not crash the others.
8. **Domain errors → HTTP exceptions**: conversion happens in routers (api/v1/*.py), not in services.
9. **First user = admin + unlim**: `auth_service.register_user` checks `COUNT(users) == 0` → `is_admin=True, plan="unlim"`.
10. **SQLite boolean quirk**: `server_default='false'` (string) in Alembic saves the literal `'false'` in SQLite, which Python treats as truthy. Always use `sa.false()` in migrations.
11. **`bcrypt<5`**: passlib 1.7.4 is incompatible with bcrypt 5.x. In requirements.txt: `bcrypt>=4.0.0,<5.0.0`.
12. **`postgres://` → `postgresql://`**: SQLAlchemy 2.x does not support the old scheme. Normalized in `db/base.py` and `alembic/env.py`.
13. **Wallet limit enforced on backend** via `plans.wallet_limit(user.plan)` in `wallet_service.py`. Returns HTTP 402 on limit exceeded. Frontend shows upgrade popup on 402.
14. **KuCoin** requires `pageSize >= 10` for deposits/withdrawals/ledgers.
15. **Bitget** deposit/withdrawal v2 endpoints return 404 — bills endpoints are used instead.
16. **`/api/wallets/all-addresses`** route must be declared BEFORE `/{wallet_id}` in FastAPI, otherwise `all-addresses` is treated as a wallet_id.
17. **`MutableHeaders`** in Starlette has no `.pop()`. To remove a header: `if "key" in headers: del headers["key"]`.
18. **Provider metadata** — `label`, `enabled`, `needs_passphrase`/`needs_api_key`, `soon` — class-level attributes on each provider. `WALLET_OPTIONS` is built automatically via `_build_wallet_options()`. To disable: `enabled = False` on the class, or `"enabled": False` in `CHAIN_META` for chains.
19. **`form.name.value`** — DO NOT use. `HTMLFormElement.name` is an IDL attribute that returns `""`. Always use `form.elements['name'].value`.
20. **Tag dropdown** rendered as a portal in `document.body` with `position: fixed` — avoids being clipped by `overflow-y: auto` on the wallet list container.
21. **`request_count`** incremented only on `/api/portfolio/balance` and `/api/portfolio/transactions` — tracks active wallet usage, not all API calls.
22. **Lighter HTTP 400** = address not registered on the platform (normal case) → treated as empty balance, NOT logged as error. All other HTTP errors from Lighter → re-raised → ProviderErrorLog.
23. **BalanceHistory** written only when Owner-tagged wallets are present in the fetch. Tags are global (user_id=NULL for system tags), but wallets have user_id — history is per-user via wallet's user_id.
24. **`_fetch_single` returns 3-tuple** `(BalanceResult|None, error_str|None, error_type|None)`. error_type used for ProviderErrorLog categorization.
25. **Certbot volumes** in docker-compose — `certbot_certs` mounted at `/etc/letsencrypt` in certbot and `/etc/nginx/certs` in nginx. Cert paths in nginx.conf: `/etc/nginx/certs/live/yourdomain.com/fullchain.pem`.
26. **No `.html` in URLs** — `serve_page()` in `app.py` maps `/{page}` → `frontend/{page}.html`. Static files (`.js`, `.svg`, `.png`) served directly. Alembic migration fork causes startup failure — always check that each migration has a unique `down_revision`.
27. **`unlim` plan** — admin-only. Backend enforces this: `PATCH /api/admin/users/{id}/plan` returns 400 if trying to assign `unlim` to a non-admin user.
28. **Tags are user-scoped** — `user_id` nullable; `NULL` = system tag. Unique constraint is `(name, user_id)` per `UQ_tag_name_user`. System tags visible to all users.
29. **`wallet_limit` in `UserOut`** — computed by `@model_validator(mode="after")` in `schemas/auth.py` via `plans.wallet_limit(self.plan)`. Returns `None` for unlimited plans. Automatically reflects any new plan added to `PLAN_LIMITS`. Never hardcode limits in frontend JS — always read from `/api/auth/me`.
30. **Gate.io earn endpoints**: `/earn/uni/holdings` and `/earn/savings/account` return 404 — not valid API v4 paths. `/earn/uni/lends` works for Uni Lending positions. Gate.io **Simple Earn Flexible** has no public REST API endpoint — balance is NOT accessible.
31. **KuCoin Futures** use a separate base domain `https://api-futures.kucoin.com` (not `api.kucoin.com`). Response currency `XBT` is normalized to `BTC`. Auth headers are the same format.
32. **Bybit Earn** (`/v5/earn/product/list?category=FlexibleSaving`) may return empty `stakedAmount` — fails silently, not logged as error. UNIFIED account balance already includes most earn products.
33. **Favicon for Google Search** requires at least 48×48px. Use `avalant_favicon-48.png` (or larger). Google ignores SVG favicons. All HTML pages link sized PNGs via `<link rel="icon" sizes="NxN">` — favicon.ico (16/32px) is fallback only. Google updates its cache with delay of days to weeks.
34. **`ArbAlert` model** — `direction` values: `"any"` | `"above"` | `"below"`. `threshold` is spread % (e.g. `0.05` = 0.05%). `last_triggered_at` used for 1h cooldown — alert won't re-fire within 1h of last trigger.
35. **Alert service** reads spreads from `get_cached_rates()` (no new HTTP calls). If `TG_BOT_TOKEN` is not set, alert service starts but silently skips all sends. User must set `tg_username` on their profile to receive messages.
36. **Orderbook endpoint** reuses the shared `_arb_http` client (persistent keepalive connections) — do NOT create a new `httpx.AsyncClient` inside the handler, it causes TCP handshake overhead on every 500ms poll.
37. **Aster price cache** — `_aster_price_cache` (5s TTL) separate from `_aster_vol_cache` (60s TTL). Both live in `arbitrage_service.py`. The price is `lastPrice` from `/ticker/24hr`; volume is `quoteVolume` from the same endpoint but cached longer.
38. **`arb.html` URL params** — `?symbol=BTC&long=binance&short=aster`. All three required; missing params render an error div instead of the page.
39. **`arb.html` theme** — dark/light toggle saved to `localStorage` key `arb-theme`. Light theme applied via `body.light` CSS class.
40. **`screener.html`** — the ↗ button on each arb row links to `/arb?symbol=...&long=...&short=...`.

---

# ═══ DEVELOPMENT CONTEXT — SESSION CONTINUITY (A→Z) ═══

This section captures live UX principles, architectural decisions, and working conventions established in recent sessions. A new Claude reading this should be able to continue work without relearning the author's preferences.

## A. User profile & communication style

- **Owner / primary user**: Ukrainian/Russian-speaking developer. Communicates in Russian (sometimes mixed with Ukrainian) — respond in Russian. English is fine for code/commits/PR text.
- **Senior level**: knows what they want, gives terse directives, expects me to fill in implementation details. Don't over-explain. Confirm intent once, then execute.
- **Aesthetic sense**: sharp, minimalist, professional. Trading-app polish. Visual noise is a dealbreaker.
- **Iteration rhythm**: many small adjustments per feature ("увеличь шрифт", "уберу", "нажимается со 2 раза") — treat each as quick surgical edits, not a redesign pass.
- **Will say "убери" / "отмени" without warning** — be ready to revert cleanly.

## B. Brand & design language

- **Brand**: `avalant_` — Inter 800, blinking green `_` cursor. Logo SVG at `/avalant-logo.svg` (full) and `/avalant_favicon.svg` (icon).
- **Dark theme is default**. Light theme exists as scaffold (see section G) but the toggle button is intentionally disabled right now (`return;` early in `theme.js`'s injectToggle path).
- **Never use emojis** in UI unless explicitly requested. Never add emojis to code files.
- **SVG icons**, thin stroke 1.35–1.7. No filled heavy icons unless it's a solid accent badge.
- **Fonts**: Inter (UI), JetBrains Mono (numbers, amounts, addresses, candles, order book prices).

## C. Color palette

### Dark (default)
```
--bg:       #0E0E11   /* page */
--surface:  #131217   /* cards */
--surface2: #17171C   /* nested */
--surface3: #202028   /* hover */
--border:   #22222A
--border2:  #3A3A50
--text:     #E6E8E3
--text2:    #9B9FAB
--text3:    #676B7E  (or #55596A in some pages)
--green:    #1AFFAB  /* neon — accent + positive */
--red:      #F87171
--yellow:   #E5C07B
--teal:     #06B6D4
--purple:   #925BD6
```

### Light (scaffold in `body.light` via `/theme.js`)
Pure black-and-white, deeper green/red so they read as text on white:
```
--bg:        #FFFFFF
--surface:   #FFFFFF
--surface2:  #F4F4F4
--surface3:  #E8E8E8
--border:    #BABABA
--border2:   #8C8C8C
--text:      #000000
--text2:     #1A1A1A
--text3:     #595959
--green:     #006B3C   /* deeper, readable on white */
--red:       #8B0000
--yellow:    #6B5011
```
**Do NOT brighten light-theme green back up** — the user explicitly chose a saturated dark green. Hover states in light mode use `#EDEDED` / `#F4F4F4` backgrounds, never inverted-black.

## D. Shared frontend modules (load on every page after `/auth.js`)

### `/toast.js` — unified notification system
- API: `toast('msg')` | `toast('msg', 'success'|'error'|'warn'|'info')` | `toast('title', type, 'subtitle')` | `toast({title, type, sub, duration})`
- Renders slide-in cards in top-right corner. Success = green ring animation; error = red; warn = yellow.
- Auto-dismiss: 4000ms success, 3200ms others. Close button `×` always present.
- Light-theme aware via `body.light` overrides baked into the injected CSS.
- Every page includes `<script src="/toast.js"></script>`. Previously app.html had its own `toast()` — deleted. Do not reintroduce per-page toast implementations.

### `/theme.js` — theme scaffold + persistence
- Reads `localStorage.theme` at load, applies `body.light` ASAP (no FOUC).
- Exposes `window.toggleTheme()` and dispatches `themechange` event for pages with charts to re-render (`arb.html` listens and re-runs `_eeApplyTheme`, `renderSpreadChart`, `renderFundChart`).
- **The toggle button auto-injection is DISABLED** (early `return;`). Theme still works if triggered manually via `toggleTheme()` in devtools or if re-enabled later. Do not re-enable without asking.

### `/auth.js` — auth wrapper (pre-existing)
- `Auth.apiFetch(path, opts)` — prepends `/api`, adds Bearer token.
- `Auth.requireAuth()`, `Auth.requireAdmin()`, `Auth.isLoggedIn()`, `Auth.getUser()`, `Auth.logout()`.

### `/navbar.js` + `/navbar.css` — shared `<app-navbar page="...">`
- Custom element. Variants: `app`, `screener`, `archive`, `profile`, `index`, `pricing`, `arb`, `login`, `register`, `checkout`.
- `arb` variant: nav links = `['app', 'pricing']` (Screener moved into infobar, Archive removed per user request). Right-side = Alerts + Fullscreen buttons (`nav-lnk` style) + avatar.
- Brand is a sibling `.brand`, nav links are `.nav-lnk` — both must have `font-family: inherit` for Inter to apply to `<button>` elements.
- `.topbar` has `position: sticky`, backdrop-blur, green under-glow via `::after`.

## E. Page inventory (frontend)

| Page | Auth | Key libs/concepts |
|------|------|-------------------|
| `/` index.html | public | landing |
| `/login`, `/register` | public | JWT issuance, HttpOnly cookie |
| `/pricing`, `/checkout` | public + auth | plan picker |
| `/app` | auth | main portfolio — balances, tags, txns, address book |
| `/archive` | auth | archived wallets |
| `/profile` | auth | plan card, balance history SVG chart, TG username |
| `/admin`, `/admin-user` | admin | KPI, user list, plan modal, provider-errors |
| `/screener` | auth | Funding + Arb tables, WS updates, exchange filter |
| `/arb` | auth | Arbitrage pair detail — see section F |
| `/404`, `/maintenance` | public | |

All URLs served without `.html` via `serve_page()` in `app.py`.

## F. `/arb` detail page — deep dive

### Layout (desktop)
Top: shared `<app-navbar page="arb">` (52px). Below: custom `.infobar` (72px, gradient bg, horizontal overflow). Main: 3-column flex — `.col-left` (40%, tabs with charts) | `.col-books` (36%, two order books) | `.col-info` (flex:1, P&L).

### Infobar content (in order)
1. **Hero block** — clickable: `RAVEUSDT` (opens symbol popover with fuzzy-search dropdown of all symbols from `/screener/funding`), live green pulse dot, exchange pills inside a bordered `.hero-exs` group. Each exchange pill opens its own popover filtered to exchanges listing the current symbol. `⇄` swap icon rotates 180° on hover, click = swap long/short in URL.
2. **Long ex-card** — green dot + exchange label, row of Fund/Ivl/Next, row of Vol/OI.
3. **Short ex-card** — red dot + same shape.
4. **Net/8h** — big (24px) metric. Formula on arb.html is `gross_funding - total_fees` (funding-only, NO price spread — user explicitly separated them).
5. **Live Spread** — big metric, accent green bg gradient. `(priceShort - priceLong)/priceLong × 100`.
6. **Alert block** — clickable, opens Alerts modal. Shows active-alert count (`tb-alert-count`).
7. **Back-to-Screener link** — full remaining width, centered, 20px regular font, left-arrow with stick (full ←), `href="/screener?mode=arb"` so user lands back on the arb tab (not funding).

### Left tabs (order matters — user defined this explicitly)
1. **Entry/Exit** (active by default) — see below.
2. **Spread History** — price-history line chart (top 55%) + funding rate history (bottom 45%).
3. **Overview** — funding tables per side, stats cards.
4. **Info** — period selector, MAX/MIN IN/OUT, median spread, gap analysis.
5. **All Rates** — funding rate across every exchange listing the symbol, with LONG/SHORT badges.

### Entry/Exit chart (the signature feature)
- Uses **TradingView Lightweight Charts v4.1.3** (CDN). Two candlestick series:
  - **In** (green): `(bidShort − askLong) / askLong × 100` — entry divergence. Positive = free money on entry.
  - **Out** (red): `(bidLong − askShort) / askShort × 100` — exit divergence. Closer to 0 = cheap exit.
  - Interpretation: strategy is **enter when In is high, exit when Out approaches 0**.
- **Timeframes**: 30s / 1m / 5m / 15m / 20m. **One candle = one TF period** (e.g. 5m TF → 5-min candles). `EE_BUCKET = {30:30, 60:60, 300:300, 900:900, 1200:1200}`.
- **Source**: `_eeHist = [{ts, inPct, outPct}]` — raw samples pushed from `sampleEntryExit()` after every orderbook update (150ms poll). Buffer cap `EE_MAX = 9000` (~22min @ 150ms).
- **Persistence**: `localStorage[ee-hist:SYMBOL:LONG:SHORT]` — last 2000 samples saved every 1s (throttled). Auto-restored on page load (filtered to <20min old). User explicitly opted NOT to have server-side history — "без истории" if browser clears.
- **VWAP by size**: input `#ee-size` + USDT/TOKEN toggle. `_vwap(levels, size, unit)` walks orderbook to compute fill-weighted average for the given size. Empty size = best-level only.
- **Pan/zoom preserved**: `_eeNeedsFit` flag. `fitContent()` called ONLY on init or TF change, never on regular updates — user can scroll back through history without the chart snapping to "now".
- **Theme-aware**: `_eeThemeColors()` returns palette dict by `body.light`. `_eeApplyTheme()` re-applies on themechange.

### Spread History + Funding Rate charts (SVG, `renderSpreadChart` + `renderFundChart`)
- Pure SVG, hand-rolled. `_svgTheme()` helper returns palette.
- `.spread-section` and `.fund-section` are `position:relative` so their tooltips are constrained to the chart (not the body — earlier the tooltip was escaping behind the navbar; this was fixed).

### Order books
- Two panels side-by-side (`col-books`), polled at **150ms** per side. `_bookInflight[side]` guard prevents queue buildup on slow exchanges (OKX/BingX).
- Endpoint: `GET /api/screener/orderbook?symbol=X&exchange=Y&limit=50`. Uses shared `_arb_http` with http/1.1 keepalive, pool of 200 connections, keepalive_expiry=30s.
- KuCoin: must use `depth20` or `depth100` (literal), with BTC→XBT mapping. Other symbols need to exist on KuCoin futures or return Invalid symbol (normal — don't log as error for symbols that just aren't listed).
- Mid price display: `asks[last] + bids[0]) / 2` with ▲/▼ arrow animation on change.

### Alerts modal
- Full-width header with green icon plate, title + subtitle, close `×`.
- Body: pair badge (symbol + dot-long ⇄ dot-short), TG warning note (yellow accent) if user has no `tg_username`, form grid (Threshold | Direction | Add), then list.
- **Add button is disabled** if `_hasTgUsername === false`. Validates threshold is positive.
- Create/toggle/delete all go through `Auth.apiFetch('/alerts...')`, all errors → `toast()`.
- Empty state: dashed circle icon + explanation.

### Mobile responsive
- Bottom-nav injected at `<768px`: Portfolio / Archive / Screener / Pricing / Profile. Glassmorphic backdrop, `position:fixed`, `body { padding-bottom: 88px }`.
- `@media (max-width: 900px)`: infobar wraps, columns stack vertically (Charts → Books → P&L), charts get fixed heights (320/260px).
- `@media (max-width: 560px)`: ex-cards one-per-row, metric-blocks full-width, books stack vertically (long on top), alert modal slides up as a bottom-sheet.

## G. `/screener` page — deep dive

### Modes
- URL param `?mode=funding|arb`. `_mode` initialized from URL. `switchMode(_mode)` called at bootstrap AND on `pageshow` (bfcache). Preload only calls `applyFilter` or `applyArb` based on mode.
- The arb row ↗ button opens `/arb?symbol=&long=&short=` in new tab. The back-link from `/arb` uses `?mode=arb` so the user returns to the arb tab, not funding.

### Exchange selector (left panel, current state)
- **Minimalist rows** (no longer colorful cards — user rejected two prior designs). Vertical stack, `gap: 1px`.
- Each row: [7px brand-color dot] [label] [14px checkmark square, green when selected].
- Unchecked: dot desaturated + opacity 0.35, label muted.
- Hover: `background: var(--surface3)` only. No transforms, no borders, no translates.
- Header: "Exchanges" + count pill (`X/13`) — green if all, red if none, yellow otherwise.
- Actions: All / None / Invert, each with tiny SVG icon.

### Bottom nav (mobile)
- 5 items matching other pages' bottom-nav. Light-theme overrides added.

## H1. Funding WS + REST backstop (≤5s per-symbol freshness SLA)

`backend/services/funding_ws/` is the hot path. Each adapter runs **two** concurrent loops per exchange:

- **WS task** (asyncio) in `_run()` — subscribes to the venue's broadcast channel, merges incoming rows into `self._rows`, stamps `_ts`. Primary sub-second updates.
- **REST backstop** in a **pure daemon `threading.Thread`** (not `asyncio.to_thread`) — calls `rest_refresh_sync()` every `rest_refresh_interval_s` (2s for bybit/okx/gate/kucoin/mexc/bitget/bingx), merges rows into `self._rows` directly, stamps `_ts`. Guarantees freshness on any symbol whose WS doesn't tick regularly (low-volume tokens, per-symbol channels like KuCoin `/contract/instrument`, rate-less feeds like MEXC `push.tickers`).

**Why pure thread, not `asyncio.to_thread`**: the fetcher runs 11 WS adapters + orderbook WS manager + dump loops + screener refresh on a single event loop. Under that load `await loop.run_in_executor(...)` took **5-6 seconds** to resume the future even though the actual HTTP call took 0.3-1s (`curl` proved it). Bypassing the event loop entirely dropped tick duration to **0.2-1.4s**. Per-symbol max age across all 10 exchanges dropped from 6-7s → **0.04-2.62s**.

**Safety**: the WS task on the event loop thread reads/writes `self._rows[sym]` by key. Python dict key-assignment is GIL-atomic, so cross-thread writes from the REST thread are safe without locks.

Dedicated sync HTTP client (`_rest_http = httpx.Client(...)`) with its own pool — **never** share with the async `_http` in `arbitrage_service.py`; they saturate each other.

Adapters that currently have REST backstops:
- **Bybit** — `/v5/market/tickers?category=linear` (was missing volume on partial updates)
- **OKX** — `/api/v5/market/tickers?instType=SWAP` (WS supplies rate; REST supplies price/volume)
- **Gate** — `/api/v4/futures/usdt/tickers`
- **KuCoin** — `/api/v1/contracts/active` (WS never delivered volume or rate — REST owns both)
- **MEXC** — `/contract/funding_rate` + `/contract/ticker` (WS has no rate; REST owns rate)
- **Bitget** — `/api/v2/mix/market/tickers?productType=USDT-FUTURES`
- **BingX** — `/openApi/swap/v2/quote/premiumIndex` + `/ticker` (WS caps at ~100 symbols; REST fills all ~600)

**Do NOT regress to async REST.** If you need to add a new REST backstop, implement `rest_refresh_sync()` (sync), not `rest_refresh()` (async). The base `_rest_loop_sync()` runs in a daemon thread and calls it.

**ping_timeout = 60s** (was == ping_interval = 20s) — older config killed healthy WS sessions under traffic spikes with 1011 keepalive errors on WhiteBit/BingX/Bitget.

## H. `/screener` perf architecture (arbitrage_service.py)

- **Two-tier cache**: `_cache` (6s TTL, price/rate) + `_ivl_cache` (1h TTL, intervals).
- **MEXC/Bitget interval fetchers** are slow (40s+ for ~500 per-symbol requests). They're in `_SLOW_IVL = {"mexc", "bitget"}` and never block user-facing requests — `_get_interval_map(ex, allow_blocking=False)` kicks off a background refresh and returns cached-or-empty. `_fetch_mexc`/`_fetch_bitget` default `interval_h = 4.0` (most common) when the cache is empty; real values fill in once the bg refresh completes.
- **Per-exchange asyncio.Lock** (`_ivl_locks`) prevents duplicate concurrent interval fetches.
- **Warmup**: broadcaster fires `asyncio.create_task(_warmup())` on startup so first user request hits warm cache.
- **OKX funding rate**: has its own `_okx_fr_cache` (5min TTL) because rates require 500 per-symbol calls.
- **Aster**: two separate caches — `_aster_price_cache` (5s) for lastPrice, `_aster_vol_cache` (60s) for quoteVolume. Also filters `SHIELD*` symbols AND non-`TRADING` status from exchangeInfo to exclude 1001x/Shield synthetic contracts.
- **Open Interest endpoints** implemented for: binance, bybit, okx, gate, hyperliquid, aster, bingx, mexc, bitget, kucoin, whitebit. Ethereal has no public OI endpoint.

## I. Arbitrage opportunity schema (what's in `_opp`)

```py
{
  symbol, long_exchange, short_exchange,
  long_rate, short_rate,        # already × (8/interval) — i.e. 8h-normalized %
  long_price, short_price,
  long_volume, short_volume,
  long_interval_h, short_interval_h,  # added recently — frontend reads these for Ivl display
  gross_funding, price_spread,
  fee_long, fee_short, total_fees,
  net_profit,                   # on screener = gross+spread-fees; on arb.html Net/8h = gross-fees only (spread shown separately)
  gross_apr, net_apr,
  valid_price, next_ts_long, next_ts_short
}
```

**Fallback**: if `/screener/arbitrage` skips a direction (because `gross ≤ 0`), arb.html synthesizes a `_opp` from `/screener/all-exchanges-funding?symbol=X` so the page still populates.

## J. API endpoints touched in recent sessions

- `GET /api/screener/funding` — all symbols × exchanges (primary load)
- `GET /api/screener/arbitrage` — computed opps (filtered gross>0)
- `GET /api/screener/all-exchanges-funding?symbol=X` — for arb.html All Rates tab + popover filter + fallback synthesis
- `GET /api/screener/orderbook?symbol=&exchange=&limit=` — 150ms polled from arb.html
- `GET /api/screener/open-interest?symbol=&long_ex=&short_ex=` — parallel fetch
- `GET /api/screener/arb-price-history?symbol=&long_ex=&short_ex=`
- `GET /api/screener/arb-history?symbol=&long_ex=&short_ex=`
- `WS /api/screener/ws/funding`, `WS /api/screener/ws/arb` — live updates
- `GET/POST/PATCH/DELETE /api/alerts` — spread alerts CRUD

## K. Backend HTTP client (`_http` in arbitrage_service.py)

Tuned for heavy orderbook polling:
```py
_http = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=5.0, read=8.0, write=5.0, pool=2.0),
    headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip, deflate"},
    follow_redirects=True,
    limits=httpx.Limits(max_connections=200, max_keepalive_connections=60, keepalive_expiry=30),
    http2=False,  # exchanges generally work better with HTTP/1.1
)
```
Used by both arbitrage_service fetchers AND the orderbook endpoint (aliased as `_arb_http`). Never create new clients per request.

## L. Workflow — git conventions

- **Conventional commits** with scope-less verbs: `feat:`, `fix:`, `style:`, `perf:`, `refactor:`, `docs:`, `chore:`.
- **Body**: bulleted list of notable sub-changes. 1–2 line summary at top.
- **Trailer**: ALWAYS include `Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>`.
- **Commit via HEREDOC** (see earlier "Creating pull requests" / "Committing" sections in this file for exact format).
- **Push**: user triggers with "коммит и пуш" — always do both in one step. Never auto-push without that (or equivalent) explicit request.
- **Never `--amend`**, never `--force-push` unless user explicitly asks. Never skip hooks.
- **Stage explicitly** — list files on `git add` rather than `git add -A`.

## M. Code style conventions

- **No comments unless non-obvious WHY** — good identifiers beat explanations. Never document what the code does.
- **No one-off abstractions** — three similar lines beats a premature helper.
- **No defensive programming inside trusted boundaries** — skip redundant null checks, optional chain everything reachable.
- **Never add backwards-compat shims** for in-development features.
- **Response length in chat**: concise. Final answers usually ≤100 words unless explaining architecture.

## N. Auto-restart conventions

- Dev: `uvicorn app:app --port 8000` (no `--reload` — user explicitly asked not to use reload). When backend changes, ALWAYS restart via `lsof -ti:8000 | xargs -r kill -9; nohup uvicorn ... &`.
- Frontend: no build step — edit HTML and hard-refresh (Cmd+Shift+R). Cache bust is manual.

## O. Local permissions

`.claude/settings.local.json` (gitignored) grants this project `Bash(*)`, `Edit(*)`, `Write(*)`, `Read(*)`, `Grep(*)`, `Glob(*)` without asking. Other projects behave normally. If ops become noisy, check there first.

## P. Known "don't touch" decisions

- **Theme toggle disabled** — don't re-enable the UI button without asking (theme.js early return in injectToggle).
- **No Screener link in arb.html navbar** — it's only in the infobar back-link. User explicitly removed it from both `_NAV_SET.arb` and the right-side actions.
- **No Archive link in arb navbar** either.
- **Light theme uses `#006B3C` green + `#8B0000` red** — user picked these for text readability on white. Do not brighten.
- **No per-exchange tinted cards** in the screener ex-selector — user rejected colorful grid; settled on monochrome rows with just a color dot.
- **No tooltips on infobar metrics** — user asked for them, then asked to remove. Don't add back unless explicitly requested.
- **No "Funding History" panel** in the arb center column — user moved it to Overview tab + Spread History tab only.
- **Net / 8h on arb.html excludes price spread** — user asked for funding-only on that metric. Screener still computes net_profit as gross+spread-fees (do not propagate the arb.html-only change to the backend).

## Q. Naming patterns

- Frontend state: `S = {...}` for app.html, ad-hoc globals elsewhere.
- Prefixed-underscore privates: `_opp`, `_eeHist`, `_mode`, `_bookInflight`, `_popState`, `_arb_http`, etc.
- CSS classes: `kebab-case`, semantic (`.hero-block`, `.ex-card`, `.lp-ex-item`, `.chart-tabs`).
- DOM IDs: kebab-case matching the feature (`tb-net-val`, `fund-long-rate`, `ee-chart`, `ap-pop`).

## R. Notable fixes / gotchas from recent sessions

- **KuCoin futures orderbook**: endpoint is `/api/v1/level2/depth20` or `depth100` (fixed depth in path). `symbol=` uses `XBT` prefix for BTC. Other base assets use their own symbol as-is.
- **MEXC / Bitget interval fetch** = 500 per-symbol requests = 40s. Default to 4.0h while the background refresh runs.
- **Aster 1001x / Shield synthetic tokens** can't be distinguished from Pro contracts via public API fields (all have tradingMode=0, symbolType=0, empty tags). We filter by symbol prefix (SHIELD*) + status != TRADING. 1001x-only tokens usually have tiny volume — but we don't filter by volume (user decided to keep showing them).
- **Aster EIP-712 auth** (provider, not screener): `encode_typed_data(full_message=td)` keyword arg in eth_account 0.13.7. Base URL `https://fapi.asterdex.com` (not fapi3, which is AWS-blocked in some regions).
- **OKX URL format** (for "open on exchange" link): `/trade-swap/{sym}-usdt-swap` (lowercase).
- **Aster deep link**: `/en/trade/pro/futures/{SYM}USDT` — user picked Pro mode path.
- **Charts tooltip escaping**: make the SVG chart's parent `position:relative` so absolutely-positioned `.chart-tooltip` anchors inside, not to body.
- **`<button>` in navbar** needs `font-family: inherit` or it renders system font.

## S. Performance budgets (what "fast" means here)

- Screener page open (cold): target ≤10s first paint, achieved via background warmup + non-blocking IVL fetchers.
- Orderbook refresh interval on arb.html: **150ms**. Not 100ms (client-side storms some exchanges), not 500ms (feels laggy).
- WS broadcast interval: 5s from server (`BROADCAST_INTERVAL`). Cache TTL 6s (slightly longer to prevent double-fetches).
- Alert service poll: 60s (set in alert_service.py).

## T. Toast usage cheat sheet

```js
toast('Saved');                                     // info
toast('Alert created', 'success');
toast('Alert created', 'success', 'RAVE · BingX / Gate · trigger at ±0.05%');
toast('Failed to create alert', 'error');
toast('Set TG username in profile first', 'warn');
toast({ title: 'Cron running', type: 'info', duration: 10000 });
```

HTML in `sub` is allowed — use sparingly (`<span class="mono">X</span>` for emphasis).

## U. Mobile nav pattern (replicate for new pages)

```html
<nav class="bottom-nav" id="bottom-nav">
  <a href="/app" class="bnav-item {active?}"><svg…/>Portfolio</a>
  …5 items total…
</nav>
```
CSS: fixed bottom, 12px inset, 64px tall, glassmorphic, `z-index:300`, backdrop-blur. Body needs `padding-bottom: 88px` at `<768px`. Light theme overrides use `rgba(255,255,255,0.92)` + `#BABABA` border.

## V. Feature flags / "soft launch" switches

- `aster_provider.py` has `soon = True` hint for perpdex metadata (some providers show a "soon" badge in UI even when functional). Double-check before presenting as production.
- `theme.js` injectToggle disabled.
- Admin-only `unlim` plan — backend enforces (see gotcha #27).
- `TG_BOT_TOKEN` missing → alert service no-ops silently.

## W. When user says X, do Y (short dictionary)

- "коммит и пуш" → `git add <specific files> && git commit -m "..." && git push`. Never `git add -A`.
- "перезапусти" / "перезапускай" → kill uvicorn and start fresh.
- "убери" → remove last added element + its CSS + its JS + any related state in one edit.
- "скинь что видно" / "выведи" → dump raw data (API response, logs, etc.) in a fenced code block.
- "сделай попривлекательнее" / "добавь жизни" → visual polish pass: spacing, typography hierarchy, micro-interactions. Not a rewrite.
- "сухо" → the user thinks current design lacks personality — add restrained polish, not color.
- "[page] тоже" / "везде" → propagate the change across all equivalent pages/components.
- "помни это" / "сохрани" → write to auto-memory or propose a CLAUDE.md addition.

## X. Files that did NOT exist before recent sessions

Add these to your mental model of the repo:
- `frontend/toast.js`
- `frontend/theme.js`
- `backend/providers/exchanges/bingx_provider.py`
- `backend/providers/exchanges/kraken_provider.py`
- `backend/providers/exchanges/whitebit_provider.py`

## Y. Debugging workflow preference

- User will paste UI state or error text verbatim. Read carefully; usually the fix is obvious from the snippet.
- Preferred diagnosis: `curl` against localhost:8000 (often already authed via cookie if browser has session). `Bash` tool + `python -c` for live API probing is normal.
- When unsure what's happening, user is fine with me running dev Python to probe exchanges directly before changing code.

## Z. Continuity contract

When a new session starts:
1. Read this file end-to-end before first action.
2. Don't re-confirm aesthetic preferences — they're locked in above.
3. Don't "clean up" things listed in section P ("don't touch").
4. If the user asks something and section W has a matching phrase, execute that pattern immediately.
5. When in doubt about scope (project-wide vs. local), default to local + tell the user.
6. End every meaningful turn with a terse status line. No trailing summaries, no "let me know if..." padding.
