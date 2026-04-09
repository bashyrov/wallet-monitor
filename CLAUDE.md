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
│   ├── avalant_favicon.png             # Browser favicon (PNG fallback)
│   ├── avalant-logo.svg                # Full logo — used in login/register form cards
│   ├── favicon.ico                     # ICO for Google Search
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
    │   ├── auth.py                     # UserRegister, UserLogin, Token, UserOut (includes is_admin, plan, plan_expires_at)
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
    │   │   ├── bybit_provider.py       # HMAC-SHA256, X-BAPI-* headers
    │   │   ├── gate_provider.py        # HMAC-SHA512, Gate.io v4
    │   │   ├── kucoin_provider.py      # base64-HMAC-SHA256, server timestamp, encrypted passphrase
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
        └── price_service.py            # get_usd_value(asset, amount) — CMC top-100 + Gate fallback, 30min cache
```

---

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/health` | — | `{"status": "ok"}` |
| POST | `/api/auth/register` | — | Register → `{access_token}` + sets HttpOnly `session` cookie |
| POST | `/api/auth/login` | — | Login → `{access_token}` + sets HttpOnly `session` cookie |
| POST | `/api/auth/logout` | — | Deletes `session` cookie |
| GET | `/api/auth/me` | Bearer | Current user (`id, username, email, is_admin, plan, plan_expires_at`) |
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
- Admin sets plan via `PATCH /api/admin/users/{id}/plan {plan, plan_expires_at}`
- `plan_expires_at` is stored but not automatically enforced (manual management)

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
- **Backend page protection** (`serve_page` in `app.py`): `_AUTH_PAGES = {"app", "profile", "archive"}`, `_ADMIN_PAGES = {"admin", "admin-user"}` — checked via HttpOnly `session` cookie
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
| `profile.html` | requireAuth + backend cookie | Profile: balance history chart, plan info with color badge, admin link |
| `login.html` | redirectIfAuthed | Login form → JWT + HttpOnly cookie → redirect to /app |
| `register.html` | redirectIfAuthed | Register form → JWT + HttpOnly cookie → redirect to /app |
| `pricing.html` | — | Basic/Pro/Platinum/Enterprise plans, monthly/annual toggle |
| `checkout.html` | — | Card payment form (stub) |
| `archive.html` | requireAuth + backend cookie | Archived wallets with restore/delete |
| `admin.html` | requireAdmin + backend cookie | KPI, users table with plan badge + plan modal, provider errors tab |
| `admin-user.html` | requireAdmin + backend cookie | Per-user detail: stats, wallet list, last active |
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
| Gate | wallet deposits + withdrawals + spot trades + futures book | HMAC-SHA512 |
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
