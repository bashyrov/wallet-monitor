# Avalant — Development Guide for Claude

## Что это за проект

**Avalant** — веб-приложение для агрегации балансов крипто-кошельков из нескольких источников. FastAPI бэкенд + multi-page vanilla JS фронтенд. БД — PostgreSQL (production) / SQLite (local dev). Миграции — Alembic.

Бренд: "avalant_" — Inter 800, 18px, с мигающим зелёным `_`.

Поддерживаемые источники:
- **8 CEX бирж**: Binance, OKX, Bybit, Gate, MEXC, KuCoin, Bitget, Backpack
- **5 Perp DEX**: Hyperliquid, Aster, Lighter, Ethereal, Paradex (заглушка)
- **13 сетей (chains)**: Tron, Ethereum, BSC, Polygon, Arbitrum, Optimism, Base, Avalanche, zkSync, Linea, Scroll, Mantle, Blast

---

## Запуск

### Локально (SQLite)
```bash
source venv/bin/activate
uvicorn app:app --reload --port 8000
# Фронтенд: http://localhost:8000
# БД: wallet_monitor.db (создаётся автоматически через Alembic)
```

### Docker (PostgreSQL)
```bash
cp .env.example .env   # заполнить переменные
docker-compose up -d
# Фронтенд: http://localhost:8000
```

**Первый пользователь** (минимальный `id`) автоматически получает `is_admin = true` при регистрации.

---

## Структура проекта

```
wallet-terminal/
├── app.py                              # FastAPI entry point: lifespan, CORS, security headers, роутеры
├── settings.py                         # Pydantic BaseSettings — конфиг из .env
├── main.py                             # Старый CLI скрипт (не используется)
├── requirements.txt
├── Dockerfile                          # python:3.13-slim, uvicorn
├── docker-compose.yml                  # PostgreSQL 16 + app, named volume postgres_data
├── alembic.ini                         # Конфиг Alembic
├── alembic/
│   ├── env.py                          # Читает DATABASE_URL из settings, нормализует postgres:// → postgresql://
│   └── versions/
│       ├── 014613d42a04_initial.py     # Таблицы: users, wallets, tags, wallet_tags, wallet_addresses
│       └── fb0ca8a11562_add_is_admin_to_users.py  # Колонка is_admin + авто-промоут первого юзера
│
├── frontend/
│   ├── auth.js                         # Общий модуль авторизации (getToken, setSession, requireAuth, requireAdmin, isAdmin, logout)
│   ├── index.html                      # Лендинг / главная страница
│   ├── app.html                        # Основное приложение — портфолио, балансы, транзакции
│   ├── profile.html                    # Профиль пользователя, статистика, план, ссылка на admin (только для is_admin)
│   ├── login.html                      # Форма входа → JWT токен → redirect в app.html
│   ├── register.html                   # Форма регистрации → JWT токен → redirect в app.html
│   ├── pricing.html                    # Страница с тарифами Free / Pro
│   ├── checkout.html                   # Форма оплаты карты (заглушка)
│   └── admin.html                      # Админ-панель — только для is_admin (requireAdmin)
│
└── backend/
    ├── crypto.py                       # Fernet-шифрование credentials: encrypt/decrypt_credentials()
    │
    ├── db/
    │   ├── base.py                     # _make_engine() (SQLite + PostgreSQL), SessionLocal, Base, get_db()
    │   └── models.py                   # ORM: User, Wallet, Tag, wallet_tags (M2M), WalletAddress
    │
    ├── domain/
    │   ├── models.py                   # Датаклассы: WalletBasic, ExchangeWallet, ChainWallet, PerpDexWallet, BalanceResult
    │   ├── enums.py                    # ExchangeType, ChainType, PerpDexType
    │   └── errors.py                   # Domain exceptions: WalletNotFound, TagNotFound, TagAlreadyExists,
    │                                   #   InvalidProviderType, InvalidCredentials, InvalidAddress, ProviderUnavailable
    │
    ├── schemas/
    │   ├── auth.py                     # UserRegister, UserLogin, Token, UserOut (включает is_admin)
    │   ├── common.py                   # TagCreate/Update/Out, WalletCreate, WalletOut, WalletAddressCreate, WalletAddressOut
    │   ├── portfolio.py                # BalanceFetchRequest, WalletBalanceResult, AggregatedBalance, BalanceResponse,
    │   │                               #   TransactionFetchRequest, Transaction (с полем address), TransactionResponse
    │   ├── wallets.py                  # ExchangeWalletSchema, ChainWalletSchema, PerpDexWalletSchema
    │   └── __init__.py                 # Re-export всего из трёх файлов
    │
    ├── providers/
    │   ├── base_wallet_provider.py     # ABC: fetch_balance(), _build_result(), _empty_details()
    │   │                               #   class attrs: name, label, enabled, needs_passphrase/needs_api_key
    │   ├── utils.py                    # STABLE_COINS tuple
    │   ├── exchanges/
    │   │   ├── __init__.py             # EXCHANGE_PROVIDERS dict {value → class}
    │   │   ├── _signing.py             # HMAC helpers: hex_hmac_sha256, b64_hmac_sha256, hex_hmac_sha512, sha512_hex, ms(), s()
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
    │   │   ├── lighter_provider.py     # Public GET /api/v1/account
    │   │   ├── ethereal_provider.py    # Public GET /v1/subaccount
    │   │   └── paradex_provider.py     # Starknet address
    │   └── chains/
    │       ├── __init__.py             # CHAIN_PROVIDERS dict + CHAIN_META {value → {label, enabled}}
    │       ├── base_chain_provider.py  # Базовый класс: label, enabled, base_url
    │       ├── evm_chains.py           # EVMChainProvider (Ankr API + plain RPC fallback)
    │       └── tron_provider.py        # TronProvider (TronGrid + KNOWN_TRC20 маппинг)
    │
    ├── api/
    │   ├── deps.py                     # get_db, get_current_user (JWT Bearer), get_admin_user (403 если не admin)
    │   └── v1/
    │       ├── router.py               # Главный APIRouter prefix="/api", подключает все sub-роутеры
    │       ├── health.py               # GET /api/health
    │       ├── auth.py                 # POST /api/auth/register, /login; GET /api/auth/me; rate limiter
    │       ├── admin.py                # GET /api/admin/stats, /users; PATCH /api/admin/users/{id}/admin
    │       ├── wallets.py              # CRUD кошельков, теги, адреса (wallet addresses)
    │       ├── tags.py                 # GET/POST/PUT/DELETE /api/tags
    │       └── portfolio.py            # POST /api/portfolio/balance, /transactions
    │
    └── services/
        ├── auth_service.py             # register_user, authenticate_user, create_token, decode_token, get_user_by_*
        ├── wallet_service.py           # CRUD кошельков, тегов, wallet addresses + all_addresses()
        ├── balance_service.py          # fetch_balances(db_wallets) → BalanceResponse (параллельно через asyncio.gather)
        ├── portfolio_service.py        # aggregate(results) — утилита пересчёта агрегата
        └── transaction_service.py      # fetch_transactions(db_wallet) → TransactionResponse (5 последних tx)
```

---

## API Endpoints

| Method | Path | Auth | Описание |
|--------|------|------|----------|
| GET | `/api/health` | — | `{"status": "ok"}` |
| POST | `/api/auth/register` | — | Регистрация → `{access_token}` |
| POST | `/api/auth/login` | — | Вход → `{access_token}` |
| GET | `/api/auth/me` | Bearer | Текущий пользователь (`id, username, email, is_admin`) |
| GET | `/api/admin/stats` | Bearer + admin | Статистика: users_count, wallets_count, by_type, recent_users |
| GET | `/api/admin/users` | Bearer + admin | Список всех пользователей с wallet count |
| PATCH | `/api/admin/users/{id}/admin` | Bearer + admin | Toggle is_admin (нельзя менять себе) |
| GET | `/api/wallets` | Bearer | Список кошельков текущего пользователя |
| POST | `/api/wallets` | Bearer | Создать кошелёк |
| DELETE | `/api/wallets/{id}` | Bearer | Удалить кошелёк |
| POST | `/api/wallets/{id}/tags/{tag_id}` | Bearer | Добавить тег кошельку |
| DELETE | `/api/wallets/{id}/tags/{tag_id}` | Bearer | Убрать тег |
| GET | `/api/wallets/options` | Bearer | Доступные типы (exchange/chain/perpdex списки) |
| GET | `/api/wallets/all-addresses` | Bearer | Все именованные адреса + chain/perpdex адреса (для address book) |
| GET | `/api/wallets/{id}/addresses` | Bearer | Список именованных адресов кошелька |
| POST | `/api/wallets/{id}/addresses` | Bearer | Добавить именованный адрес `{name, address}` |
| DELETE | `/api/wallets/{id}/addresses/{addr_id}` | Bearer | Удалить именованный адрес |
| GET | `/api/tags` | Bearer | Список тегов |
| POST | `/api/tags` | Bearer | Создать тег |
| PUT | `/api/tags/{id}` | Bearer | Обновить тег |
| DELETE | `/api/tags/{id}` | Bearer | Удалить тег |
| POST | `/api/portfolio/balance` | Bearer | Балансы `{"wallet_ids": [1,2,3]}` — пустой список = все |
| POST | `/api/portfolio/transactions` | Bearer | Последние 5 транзакций `{"wallet_id": 1}` |

---

## Аутентификация

JWT Bearer-токены (`python-jose`, HS256). Пароли — bcrypt (`passlib[bcrypt]`, `bcrypt>=4,<5`).

```
POST /api/auth/register {username, email, password}
  → auth_service.register_user(db, username, email, password)
    → bcrypt.hash(password) → User(is_admin=True если первый юзер)
    → return Token(access_token=create_token(user.id))

POST /api/auth/login {login, password}   # login = username ИЛИ email
  → auth_service.authenticate_user(db, login, password)
  → bcrypt.verify(password, hashed) → Token

GET /api/auth/me   Authorization: Bearer <token>
  → deps.get_current_user → decode_token → User
```

**Rate limiting на `/api/auth/*`**: in-memory, per IP, 10 попыток / 60 сек → 429. Очищается при успешном входе. `X-Forwarded-For` поддерживается.

---

## База данных

PostgreSQL (production) / SQLite (local). Миграции — Alembic, запускаются автоматически при старте (`alembic upgrade head`).

### Таблица `users`
| Поле | Тип | Описание |
|------|-----|----------|
| id | Integer PK | |
| username | String UNIQUE | |
| email | String UNIQUE | |
| hashed_password | String | bcrypt |
| is_admin | Boolean | default False; первый юзер → True |
| created_at | DateTime | |

### Таблица `wallets`
| Поле | Тип | Описание |
|------|-----|----------|
| id | Integer PK | |
| name | String | min 6 символов |
| wallet_type | String | `exchange` / `chain` / `perpdex` |
| type_value | String | `binance`, `tron`, `hyperliquid`, ... |
| credentials | JSON | **Fernet-зашифрованные** значения: `{api_key, api_secret, api_passphrase?}` или `{address}` |
| user_id | Integer FK → users | nullable (legacy) |
| created_at | DateTime | |

### Таблица `tags`
| Поле | Тип | Описание |
|------|-----|----------|
| id | Integer PK | |
| name | String UNIQUE | |
| color | String | hex `#RRGGBB` |

### Таблица `wallet_tags` (M2M)
wallet_id + tag_id, CASCADE DELETE

### Таблица `wallet_addresses`
| Поле | Тип | Описание |
|------|-----|----------|
| id | Integer PK | |
| wallet_id | Integer FK → wallets | CASCADE DELETE |
| name | String | Пользовательский лейбл, напр. "Binance SOL" |
| address | String | On-chain адрес |
| created_at | DateTime | |

---

## Шифрование credentials (`backend/crypto.py`)

Все строковые значения в `credentials` JSON шифруются Fernet при сохранении и расшифруются при чтении.

- Ключ: PBKDF2-SHA256 из `settings.ENCRYPTION_KEY`, 260 000 итераций, salt = `b"wallet-monitor-creds-v1"`
- `encrypt_credentials(creds: dict) → dict` — шифрует все str значения
- `decrypt_credentials(creds: dict) → dict` — расшифровывает, graceful fallback на plain text (legacy)
- `WalletOut.display_info` — маскированное представление (напр. `abcd****wxyz`), credentials в API никогда не возвращаются в открытом виде

---

## Безопасность

### app.py
- **CORS**: `CORSMiddleware`, настраивается через `ALLOWED_ORIGINS` (comma-separated или пустая строка = same-origin only)
- **Security headers**: `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `X-XSS-Protection: 1; mode=block`, `Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy`; удаление `Server` заголовка
- **OpenAPI скрыт**: `docs_url=None, redoc_url=None, openapi_url=None`
- **_check_security()**: предупреждение при использовании дефолтных `SECRET_KEY` / `ENCRYPTION_KEY`

### Доступ
- **`get_current_user`** (`deps.py`): проверяет Bearer JWT, 401 если отсутствует/невалиден
- **`get_admin_user`** (`deps.py`): оборачивает `get_current_user`, 403 если `is_admin=False`
- **Фронтенд** (`auth.js`): `requireAuth()` — редирект на `/login.html`; `requireAdmin()` — редирект на `/app.html`; `admin.html` защищён `requireAdmin()`; ссылка на admin в `profile.html` скрыта если `!is_admin`

---

## Как работает провайдер-система

### Балансы
```
POST /api/portfolio/balance
  → balance_service.fetch_balances(db_wallets)
    → asyncio.gather(_fetch_single(w) for w in wallets)   # return_exceptions=True
      → _fetch_single(db_wallet):
          1. decrypt_credentials(db_wallet.credentials)
          2. Валидирует через Pydantic (ExchangeWalletSchema / ChainWalletSchema / ...)
          3. Создаёт domain объект (ExchangeWallet / ChainWallet / ...)
          4. domain.__post_init__ → _resolve_provider() → ищет в PROVIDERS dict
          5. provider = wallet.provider()  # создаёт instance
          6. await provider.fetch_balance(wallet) → BalanceResult
          7. aclose() в finally
```

### Транзакции
```
POST /api/portfolio/transactions
  → transaction_service.fetch_transactions(db_wallet)
    → dispatcher по wallet_type / type_value
      → специфичная async функция (_binance_txs, _okx_txs, ...)
        → decrypt_credentials → возвращает list[Transaction] (max 5)
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

### Transaction
```python
class Transaction(BaseModel):
    tx_id: str
    type: str       # deposit / withdraw / trade / fill / transfer / contract
    asset: str
    amount: str
    timestamp: str  # "YYYY-MM-DD HH:MM"
    status: str     # completed / pending / failed
    address: str | None  # адрес контрагента (from/to) — для address book matching
```

---

## Система метаданных провайдеров

Каждый провайдер — это класс с атрибутами, которые автоматически попадают в `/api/wallets/options`.

**Атрибуты на классе:**
```python
class BinanceProvider(BaseWalletProvider):
    name = "BinanceProvider"   # internal ID
    label = "Binance"          # отображается в UI
    enabled = True             # False → скрыт из UI и не используется
    needs_passphrase = False   # для exchange-провайдеров
    needs_api_key = False      # для perpdex-провайдеров
    soon = True                # для perpdex — показывает "soon" badge
```

**Chain-провайдеры** используют один класс на все EVM-сети, поэтому метаданные хранятся в `CHAIN_META` (`chains/__init__.py`):
```python
CHAIN_META: dict[str, dict] = {
    "ethereum": {"label": "Ethereum", "enabled": True},
    # enabled=False → цепь скрыта из UI
}
```

**`WALLET_OPTIONS`** в `wallets.py` генерируется автоматически из провайдеров через `_build_wallet_options()`. Ручного редактирования не требуется — достаточно поменять атрибуты класса.

---

## Как добавить новый провайдер

### Новая биржа (Exchange)
1. Создать `backend/providers/exchanges/newexchange_provider.py`, наследовать `BaseWalletProvider`
2. Задать class-атрибуты: `name`, `label`, `enabled = True`, `needs_passphrase`
3. Реализовать `fetch_balance(wallet) → BalanceResult`, использовать `self._build_result(...)`
4. Добавить в `EXCHANGE_PROVIDERS` в `backend/providers/exchanges/__init__.py`
5. Добавить в `ExchangeType` в `backend/domain/enums.py`
6. Добавить функцию `_newexchange_txs(creds)` в `transaction_service.py` и подключить в dispatcher
7. ~~Редактировать `wallets.py`~~ — не нужно, опции генерируются автоматически

### Новая сеть (Chain)
1. Создать провайдер в `backend/providers/chains/`, наследовать `BaseChainProvider`
2. Добавить в `CHAIN_PROVIDERS` в `backend/providers/chains/__init__.py`
3. Добавить запись в `CHAIN_META` (`{"label": "Name", "enabled": True}`)
4. Добавить в `ChainType` enum
5. Добавить RPC URL в `settings.py` как `str | None = None`

### Новый Perp DEX
1. Создать `backend/providers/perp_dexes/new_provider.py`
2. Задать class-атрибуты: `name`, `label`, `enabled = True`, `needs_api_key`, `soon` (если нужно)
3. Добавить в `PERPDEX_PROVIDERS` в `backend/providers/perp_dexes/__init__.py`
4. Добавить в `PerpDexType` enum
5. Добавить функцию `_newdex_txs(address)` в `transaction_service.py`

### Отключить провайдер
Поставить `enabled = False` на классе (exchange/perpdex) или `"enabled": False` в `CHAIN_META` (chain). Провайдер исчезнет из UI и не будет попадать в `/api/wallets/options`.

---

## settings.py — переменные окружения

```env
# База данных
DATABASE_URL=postgresql://user:pass@localhost:5432/avalant  # SQLite по умолчанию

# Безопасность (ОБЯЗАТЕЛЬНО переопределить в production!)
SECRET_KEY=change-me-in-production-use-a-long-random-string
ENCRYPTION_KEY=change-me-in-production-use-a-long-random-string
ACCESS_TOKEN_EXPIRE_DAYS=30

# CORS (пустая строка = same-origin only)
ALLOWED_ORIGINS=https://yourdomain.com,https://www.yourdomain.com

# EVM RPC URLs (нужны только если нет ANKR_KEY)
ETHEREUM_RPC=   BSC_RPC=       POLYGON_RPC=   ARBITRUM_RPC=
OPTIMISM_RPC=   BASE_RPC=      AVALANCHE_RPC= ZKSYNC_RPC=
LINEA_RPC=      SCROLL_RPC=    MANTLE_RPC=    BLAST_RPC=

# Ankr — даёт все ERC-20 токены для всех EVM цепей (рекомендуется)
ANKR_KEY=

# Tron — TronGrid Pro key (без него работает, но rate limit ниже)
TRON_KEY=
TRON_RPC=
```

**EVM балансы**: `ANKR_KEY` → `ankr_getAccountBalance` (все токены). Без него → `eth_getBalance` (только native).
**EVM транзакции**: `ANKR_KEY` → `ankr_getTokenTransfers` → `ankr_getTransactionsByAddress` (fallback). Без него — пустой список.

---

## Фронтенд

Несколько HTML-страниц, каждая self-contained (inline CSS + JS). Общий дизайн-язык. Все страницы используют `auth.js` для авторизации.

### Страницы
| Файл | Auth guard | Описание |
|------|-----------|----------|
| `index.html` | — | Лендинг |
| `app.html` | requireAuth | Основное приложение: список кошельков, проверка балансов, история транзакций |
| `profile.html` | requireAuth | Профиль: статистика кошельков, план (Free/Pro), ссылка на admin только для is_admin |
| `login.html` | redirectIfAuthed | Форма входа → JWT → redirect в app.html |
| `register.html` | redirectIfAuthed | Форма регистрации + выбор тарифа → JWT → redirect в app.html |
| `pricing.html` | — | Тарифы Free ($0, 3 кошелька) и Pro ($9/mo), FAQ |
| `checkout.html` | — | Форма оплаты карты (заглушка) |
| `admin.html` | requireAdmin | KPI grid, sparklines, план-donut, таблица пользователей, активность |

### auth.js — API
```javascript
Auth.getToken()           // Bearer токен из localStorage
Auth.getUser()            // Decoded user {id, username, email, is_admin}
Auth.setSession(token, user)  // Сохраняет в localStorage
Auth.clearSession()       // Очищает localStorage
Auth.isLoggedIn()         // Проверяет наличие токена
Auth.isAdmin()            // is_admin из сохранённого user
Auth.requireAuth(redirect)    // Редирект на /login.html если не залогинен
Auth.requireAdmin(redirect)   // Редирект на /app.html если не admin
Auth.redirectIfAuthed(redirect) // Редирект если уже залогинен
Auth.logout()             // clearSession + redirect
Auth.apiFetch(url, opts)  // fetch с Bearer заголовком
```

### Дизайн-система (Token Terminal палитра)
```css
--bg:       #0E0E11   /* основной фон */
--surface:  #131217   /* карточки */
--surface2: #17171C   /* вложенные элементы */
--surface3: #202028   /* hover состояния */
--border:   #22222A   /* все бордеры */
--text:     #E6E8E3   /* основной текст */
--text2:    #9B9FAB   /* вторичный */
--text3:    #676B7E   /* muted */
--green:    #1AFFAB   /* акцент, CTA, positive */
--teal:     #06B6D4   /* chain тип */
--purple:   #925BD6   /* perpdex тип */
--yellow:   #E5C07B   /* exchange тип */
--red:      #F87171   /* ошибки, negative */
```

### Шрифты
- **Inter** (Google Fonts) — весь UI
- **JetBrains Mono** — числа, адреса, суммы

### app.html — JS структура
```javascript
FREE_WALLET_LIMIT = 3  // лимит бесплатного плана; при достижении → openUpgradePopup()

S = {
  wallets, tags, options,
  selected: Set,          // выбранные wallet_id для проверки баланса
  tagFilter,              // активный тег-фильтр
  walletType,             // 'exchange' | 'chain' | 'perpdex' | null — открытый аккордеон
  addrPanelOpen,          // wallet_id с открытой панелью адресов (или null)
  addressBook,            // { "0xabc...": { label, walletName } } — для подсветки tx
  results, loadingBalance, lastChecked
}

api.get/post/del/put()      // Auth.apiFetch обёртки к /api/*
init()                      // загружает wallets + tags + options параллельно, затем loadAddressBook()
loadAddressBook()           // GET /api/wallets/all-addresses → строит S.addressBook
renderAll()                 // renderTagFilters() + renderWallets()
renderResults()             // shimmer → result cards с animateCounter()
checkBalance()              // POST /api/portfolio/balance
toggleTxPanel(walletId)     // раскрывает аккордеон транзакций, lazy-fetch через POST /api/portfolio/transactions
toggleAddrPanel(e, walletId)// открывает inline-панель именованных адресов у кошелька
addWalletAddr(walletId)     // POST /api/wallets/{id}/addresses
delWalletAddr(walletId, addrId) // DELETE /api/wallets/{id}/addresses/{addr_id}
togglePanel()               // сворачивает/разворачивает левую панель кошельков
openAddWalletModal()        // проверяет лимит → openUpgradePopup() или модалка добавления

// Add Wallet Modal — аккордеон
selectWalletType(type)      // открывает нужный аккордеон-раздел (exchange/chain/perpdex)
renderProtoGrid(id, name, options, cb)  // рисует чипы выбора протокола вместо <select>
_resetProtoChips()          // сбрасывает выбор чипов при открытии модалки

// Confirm popup (универсальный)
openConfirm({title, sub, name, onConfirm})  // кастомный попап вместо confirm()
closeConfirm()
```

### Модалка добавления кошелька (Add Wallet)
Трёхсекционный аккордеон (Exchange / Chain / Perp DEX). Клик на заголовок секции открывает её и закрывает предыдущую. Выбор конкретного протокола — кликабельные чипы (`proto-chip`) вместо `<select>`. Скрытые `<input type="hidden" name="exchange_type|chain_type|perpdex_type">` содержат текущий выбор. Цвет акцента меняется по типу: yellow / teal / purple.

Валидация адресов при сабмите:
- EVM: `/^0x[0-9a-fA-F]{40}$/i`
- Tron: `/^T[1-9A-HJ-NP-Za-km-z]{33}$/`
- Starknet (Paradex): `/^0x[0-9a-fA-F]{1,64}$/i`
- API key/secret: печатаемые ASCII, min 8 символов

### Кастомные confirm-попапы
Все деструктивные действия используют `openConfirm()` вместо нативного `confirm()`:
- Удаление кошелька (`delWallet`)
- Удаление тега (`delTag`)
- Удаление из архива (`deleteWallet` в `archive.html`)

### Спецэффекты в app.html
- Левая панель кошельков: `width: 360px → 0` с `transition: width 0.32s cubic-bezier(0.16,1,0.3,1)`
- Кнопка toggle панели: зелёная, SVG-стрелка поворачивается 180° при collapse
- Wallet rows: stagger animation `animation-delay: ${idx * 30}ms`
- Result cards: `border-left: 2px solid` с цветом типа (yellow/teal/purple)
- Баланс counter: анимация ease-out-quart от 0 до значения
- TX address match: `tx-row.addr-match` — зелёная подсветка + badge `⟶ Label`
- Upgrade popup: при попытке добавить 4-й кошелёк, progress bar + ссылка на /pricing.html
- Аккордеон типов: `.acc-content` max-height 0→700px, cubic-bezier анимация

---

## Транзакции (transaction_service.py)

Каждый провайдер имеет свою async-функцию. Стратегия: сначала депозиты/выводы, потом торговая активность как fallback.

| Провайдер | Источники (приоритет) | Аутентификация |
|-----------|----------------------|----------------|
| Binance | spot deposits + withdrawals + futures income (`/fapi/v1/income`) | HMAC-SHA256 |
| OKX | asset deposit-history + withdrawal-history + trade fills | base64-HMAC + server ts + passphrase |
| Bybit | deposit query-record + withdraw query-record + transaction-log | HMAC-SHA256 X-BAPI-* |
| Gate | wallet deposits + withdrawals + spot trades + futures book | HMAC-SHA512 |
| KuCoin | `/api/v1/deposits` + `/api/v1/withdrawals` + ledgers (pageSize≥10) | base64-HMAC + server ts + passphrase |
| MEXC | deposit hisrec + withdraw history | HMAC-SHA256 |
| Bitget | spot account bills + USDT/USDC futures bills | base64-HMAC + passphrase |
| Backpack | deposits + withdrawals + fills history | Ed25519 |
| Hyperliquid | `POST /info {"type": "userFills"}` | Публичный |
| Lighter | order-history (fills 403) | Публичный |
| Ethereal | fills via subaccount_id + orders fallback | Публичный |
| EVM chains | `ankr_getTokenTransfers` (primary) → `ankr_getTransactionsByAddress` (fallback) | ANKR_KEY |
| Tron | `/v1/accounts/{addr}/transactions/trc20` | TRON_KEY (опционально) |

Транзакции нормализуются в `Transaction` модель. Кэшируются на фронтенде в `_txCache[walletId]`.
Поле `address` (контрагент) заполняется где доступно: Binance deposits/withdrawals, OKX, Bybit, EVM, Tron.

---

## Address Book (именованные адреса)

Фича позволяет привязать произвольные on-chain адреса к любому кошельку с пользовательским лейблом.

**Типичный use-case**: добавить депозитный адрес Binance для Solana как "Binance SOL" → при просмотре транзакций на Solana-кошельке входящий перевод с этого адреса подсветится зелёным с лейблом "Binance SOL".

**Как работает matching**:
1. `GET /api/wallets/all-addresses` возвращает: именованные адреса из `wallet_addresses` + адреса из credentials chain/perpdex кошельков
2. Фронтенд строит `S.addressBook` (map `address.toLowerCase() → { label, walletName }`)
3. При рендере транзакций: если `tx.address` (контрагент) есть в addressBook → `.addr-match` класс + зелёный badge

---

## Что ещё не сделано (roadmap)

- [ ] **Кэширование балансов** — сохранять последний результат в БД, показывать при старте
- [ ] **Solana провайдер** — `SOLANA_RPC` в settings уже есть, провайдер не написан
- [ ] **Уведомления** — alert если баланс изменился > N%
- [ ] **История балансов** — сохранять снэпшоты в БД, строить графики
- [ ] **Редактирование кошелька** — сейчас только создание и удаление
- [ ] **Экспорт** — CSV/JSON экспорт балансов
- [ ] **Поиск** по кошелькам в левой панели
- [ ] **Fantom** — есть в `CHAIN_PROVIDERS`, но нет в `CHAIN_META` → добавить запись в `CHAIN_META` чтобы появился в UI
- [ ] **Платёжная система** — checkout.html заглушка
- [ ] **Wallet limit enforcement на бэкенде** — сейчас только на фронтенде

---

## Важные детали

1. **`wallet.provider`** — это **класс**, не инстанс. `wallet.provider()` создаёт инстанс.
2. **Пассфраза** нужна только для: OKX, KuCoin, Bitget. Определяется через `needs_passphrase = True` на классе.
18. **Provider metadata** — `label`, `enabled`, `needs_passphrase`/`needs_api_key`, `soon` — class-атрибуты на каждом провайдере. `WALLET_OPTIONS` строится автоматически через `_build_wallet_options()`. Для отключения провайдера — `enabled = False` на классе, для chain — `"enabled": False` в `CHAIN_META`.
19. **`form.name.value`** — НЕ использовать. `HTMLFormElement.name` — IDL-атрибут, возвращает `""`. Всегда `form.elements['name'].value`.
3. **TRX decimals** — 6 (SUN). USDT TRC20 — тоже 6. USDD — 18.
4. **`credentials` в БД** — Fernet-зашифрованы. Ключ деривируется из `ENCRYPTION_KEY` через PBKDF2.
5. **Двойной `aclose()`** — некоторые провайдеры вызывают `aclose()` внутри `fetch_balance`. httpx обрабатывает повторный close без ошибок.
6. **`name` валидация** — минимум 6 символов (в `WalletCreate` и `WalletBasicSchema`).
7. **`return_exceptions=True`** в `asyncio.gather` — ошибка одного провайдера не ронает остальные.
8. **Domain errors** → HTTP exceptions: конвертация происходит в роутерах (api/v1/*.py), не в сервисах.
9. **Первый юзер = admin**: `auth_service.register_user` проверяет `COUNT(users) == 0` → `is_admin=True`. Дублируется в Alembic миграции (`UPDATE users SET is_admin = true WHERE id = (SELECT MIN(id) FROM users)`).
10. **SQLite boolean quirk**: `server_default='false'` (строка) в Alembic сохраняет литерал `'false'` в SQLite, который Python воспринимает как truthy. Всегда использовать `sa.false()` в миграциях.
11. **`bcrypt<5`**: passlib 1.7.4 несовместима с bcrypt 5.x. В requirements.txt: `bcrypt>=4.0.0,<5.0.0`.
12. **`postgres://` → `postgresql://`**: SQLAlchemy 2.x не поддерживает старую схему. Нормализация в `db/base.py` и `alembic/env.py`.
13. **`FREE_WALLET_LIMIT = 3`** определён в `app.html` на фронтенде, бэкенд этот лимит не проверяет.
14. **KuCoin** требует `pageSize >= 10` для deposits/withdrawals/ledgers — нельзя запрашивать меньше.
15. **Bitget** deposit/withdrawal endpoints v2 возвращают 404 — используются bills endpoints.
16. **`/api/wallets/all-addresses`** роут должен быть объявлен ДО `/{wallet_id}` в FastAPI, иначе `all-addresses` трактуется как wallet_id.
17. **`MutableHeaders`** в Starlette не имеет `.pop()`. Для удаления заголовка: `if "key" in headers: del headers["key"]`.
